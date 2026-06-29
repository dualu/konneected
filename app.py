import sys
import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
#
from models import (
    db, 
    ShiftReport, 
    GeneratorLog, 
    FuelLog, 
    ReeferInventory, 
    ReeferFault, 
    MaintenanceTask
)
# trigger redeploy

# 1. INITIALIZE APP & CONFIG
app = Flask(__name__)
app.config['SECRET_KEY'] = 'enterprise_super_secret_key_2024'
# Fetch Neon database URL from environment variables, fallback to local if empty
database_url = os.environ.get('DATABASE_URL')

if database_url:
    # Ensure the prefix uses modern SQLAlchemy syntax ('postgresql://' vs 'postgres://')
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    print("DATABASE CONNECTION STATUS: Connecting to Neon cloud PostgreSQL database.", file=sys.stderr)
else:
    database_url = 'sqlite:///powerhouse_enterprise.db'
    print("DATABASE CONNECTION STATUS: WARNING! 'DATABASE_URL' environment variable not found. Falling back to local SQLite.", file=sys.stderr)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join('static', 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# 2. INITIALIZE EXTENSIONS
from models import db 
db.init_app(app)
migrate = Migrate(app, db)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = "Please log in to access the Powerhouse Terminal."

# 3. MODELS & USER LOADER
from models import User, SystemSettings, ShiftReport, ReeferInventory, ReeferFault, GeneratorLog, FuelLog, TaskLog, MaintenanceTask

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# 4. AUTO-BUILD DATABASE
with app.app_context():
    db.create_all()
    # Ensure Admin and Settings exist
    if not User.query.filter_by(username='admin').first():
        master_admin = User(username='admin', password_hash=generate_password_hash('admin123'), role='super_admin')
        db.session.add(master_admin)
        db.session.commit()
    if not SystemSettings.query.first():
        default_settings = SystemSettings(company_name="Powerhouse Enterprise", logo_path="default_logo.png")
        db.session.add(default_settings)
        db.session.commit()

# 5. ROUTES
@app.route('/', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        # 🔐 Redirect authenticated users who still haven't changed their password
        if current_user.must_change_password:
            return redirect(url_for('force_change_password_view'))
            
        if current_user.role == 'super_admin': return redirect(url_for('admin_dashboard'))
        if current_user.role == 'supervisor': return redirect(url_for('supervisor_dashboard'))
        return redirect(url_for('operator_dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            
            # 🔐 INTERCEPT BLOCK: Catch passwords flagged for updates immediately
            if user.must_change_password:
                flash('⚠️ Security Requirement: Please update your temporary password before continuing.')
                return redirect(url_for('force_change_password_view'))
            
            # If the user is clear, resume normal dashboard routing rules
            if user.role == 'super_admin': return redirect(url_for('admin_dashboard'))
            if user.role == 'supervisor': return redirect(url_for('supervisor_dashboard'))
            return redirect(url_for('operator_dashboard'))
        else:
            flash('Invalid username or password.')
            
    return render_template('login.html')

@app.route('/force_change_password', methods=['GET', 'POST'])
@login_required
def force_change_password_view():
    if request.method == 'POST':
        new_password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if new_password != confirm_password:
            flash("❌ Passwords do not match. Please verify your inputs.")
            return render_template('force_change.html')
            
        if new_password == "Reset123!":
            flash("❌ Security rule: You cannot reuse the temporary system password.")
            return render_template('force_change.html')

        try:
            current_user.password_hash = generate_password_hash(new_password)
            current_user.must_change_password = False 
            db.session.commit()
            
            flash("✅ Security profile updated successfully! Welcome to the system.")
            
            # 🎯 SMART DYNAMIC ROUTING: Sends each role where they belong!
            if current_user.role == 'super_admin':
                return redirect(url_for('admin_dashboard'))
            elif current_user.role == 'supervisor':
                return redirect(url_for('supervisor_dashboard'))
            else:
                return redirect(url_for('operator_portal'))
            
        except Exception as e:
            db.session.rollback()
            flash(f"🚨 Error updating security record: {str(e)}")
            
    return render_template('force_change.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# --- OPERATOR SECTION ---

# STEP 1: Paste this new Gateway route right here
@app.route('/operator')
@login_required
def operator_portal():
    if current_user.role not in ['operator', 'super_admin', 'supervisor']:
        flash("Unauthorized access role.")
        return redirect(url_for('login'))
    return render_template('shift_selection.html')


# STEP 2: Your exact loop starts here, we just modified the route string and added (shift_type)
@app.route('/operator/form/<shift_type>')
@login_required
def operator_dashboard(shift_type):
    if shift_type not in ['Day', 'Night']:
        return redirect(url_for('operator_portal'))

    system_settings = SystemSettings.query.first()
    
    next_service_data = {}
    remaining_hours_data = {}
    alert_status_data = {}  # Stores: 'normal', 'alert', or 'critical'
    
    for gen_id in ['ag2', 'ag4', 'ag5', 'ag7', 'ag8', 'ag9']:
        clean_num = gen_id.replace('ag', '')
        
        possible_ids = [
            gen_id.lower(),          # 'ag2'
            gen_id.upper(),          # 'AG2'
            f"ag-{clean_num}",       # 'ag-2'
            f"AG-{clean_num}"        # 'AG-2'
        ]
        
        # Fetch all historical logs for this unit, newest to oldest
        logs = GeneratorLog.query.filter(GeneratorLog.genset_id.in_(possible_ids)).order_by(GeneratorLog.id.desc()).all()
        
        next_srv_val = None
        # 1. FIXED: Native lookup to find the last valid Next Service Target
        for log in logs:
            if log.next_service is not None and str(log.next_service).strip() != "":
                try:
                    next_srv_val = float(log.next_service)
                    break
                except ValueError:
                    continue
                
        # 2. Scan backward to find the most recent recorded Run Hours 
        latest_run_hours = None
        for log in logs:
            if log.run_hours is not None and str(log.run_hours).strip() != "":
                try:
                    latest_run_hours = float(log.run_hours)
                    break
                except ValueError:
                    continue
        
        # 3. Apply countdown logic and evaluate thresholds
        val_display = ""
        rem_display = "N/A"
        status = "normal"
        
        if next_srv_val is not None:
            val_display = int(next_srv_val) if next_srv_val.is_integer() else next_srv_val
            
            if latest_run_hours is not None:
                remaining = next_srv_val - latest_run_hours
                rem_display = round(remaining, 1)
                
                if remaining <= 49:
                    status = "critical"
                elif remaining <= 99:
                    status = "alert"
                    
        # Bulletproof dictionary mapping: store values under every possible key style
        for key in [clean_num, int(clean_num), gen_id, gen_id.upper(), f"AG-{clean_num}"]:
            next_service_data[key] = val_display
            remaining_hours_data[key] = rem_display
            alert_status_data[key] = status

  
    return render_template('operator.html', 
                           settings=system_settings, 
                           current_user=current_user,
                           next_service_data=next_service_data,
                           remaining_hours_data=remaining_hours_data,
                           alert_status_data=alert_status_data,
                           shift_type=shift_type) # 🌟 ENSURE THIS EXACT LINE IS HERE


@app.route('/submit_shift', methods=['POST'])
@login_required
def submit_shift():
    # 📅 1. GRAB THE OPERATOR'S PICKED DATE FROM THE FORM
    date_str = request.form.get('report_date')  
    chosen_shift = request.form.get('shift_type', 'Day')
    
    # Default fallback to current UTC time if the picker is somehow empty
    report_timestamp = datetime.utcnow() 
    if date_str:
        try:
            # Convert the 'YYYY-MM-DD' text string from HTML into a real Python datetime object
            report_timestamp = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            pass  # Fall back to default if format is invalid

    # 💾 2. CREATE REPORT WITH THE CHOSEN BACKDATE
    new_report = ShiftReport(
        submitted_by=current_user.username,
        timestamp=report_timestamp,
        shift_type=chosen_shift
    )
    db.session.add(new_report)
    db.session.flush()
    
    # Reefer Data
    reefer_data = ReeferInventory(
        report_id=new_report.id,
        start_count=request.form.get('reefer_start'),
        received=request.form.get('reefer_received'),
        delivered=request.form.get('reefer_delivered'),
        end_count=request.form.get('reefer_end')
    )
    db.session.add(reefer_data)
    
    # Faulty Reefers
    i = 1
    while True:
        unit_id = request.form.get(f'fault_id_{i}')
        if not unit_id: break
        if unit_id.strip() != "":
            db.session.add(ReeferFault(
                report_id=new_report.id, 
                reefer_id=unit_id.strip(), 
                setpoint=request.form.get(f'fault_setpoint_{i}'), 
                supply_temp=request.form.get(f'fault_supply_{i}'), 
                return_temp=request.form.get(f'fault_return_{i}'), 
                alarm_code=request.form.get(f'fault_alarm_{i}')
            ))
        i += 1

    active_maintenance_alerts = []

    # Generators
    for gen_id in ['ag2', 'ag4', 'ag5', 'ag7', 'ag8', 'ag9']:
        clean_num = gen_id.replace('ag', '')
        possible_ids = [gen_id.upper(), f"AG-{clean_num}"]
        
        h = request.form.get(f'{gen_id}_hours')
        next_srv = request.form.get(f'{gen_id}_next_service')

        log_entry = GeneratorLog(
            report_id=new_report.id, 
            genset_id=gen_id.upper(), 
            volts=request.form.get(f'{gen_id}_volts') or None, 
            amps=request.form.get(f'{gen_id}_amps') or None, 
            load_pct=request.form.get(f'{gen_id}_load') or None, 
            kw=request.form.get(f'{gen_id}_kw') or None, 
            battery_v=request.form.get(f'{gen_id}_batt') or None, 
            temp_c=request.form.get(f'{gen_id}_temp') or None, 
            run_hours=h or None
        )
        
        # Fetch previous log for inheritance and SFC delta calculation
        prev_log = GeneratorLog.query.filter(GeneratorLog.genset_id.in_(possible_ids)).order_by(GeneratorLog.id.desc()).first()

        # Inheritance logic: If left blank, pull old target hours forward
        final_next_service = None
        if next_srv and next_srv.strip() != "":
            final_next_service = float(next_srv)
        else:
            if prev_log and prev_log.next_service is not None and str(prev_log.next_service).strip() != "":
                try:
                    final_next_service = float(prev_log.next_service)
                except ValueError:
                    final_next_service = None

        if final_next_service is not None:
            if final_next_service.is_integer():
                log_entry.next_service = str(int(final_next_service))
            else:
                log_entry.next_service = str(final_next_service)
                
        db.session.add(log_entry)

        # Real-time alert threshold checking during submission
        current_h = float(h) if (h and h.strip() != "") else None
        if current_h is None:
            if prev_log and prev_log.run_hours is not None and str(prev_log.run_hours).strip() != "":
                try:
                    current_h = float(prev_log.run_hours)
                except ValueError:
                    current_h = None

        if final_next_service is not None and current_h is not None:
            remaining = final_next_service - current_h
            if remaining <= 49:
                active_maintenance_alerts.append(f"⚠️ CRITICAL: Unit AG-{clean_num} has only {round(remaining, 1)} runtime hours remaining before service!")
            elif remaining <= 99:
                active_maintenance_alerts.append(f"🚨 ALERT: Unit AG-{clean_num} has only {round(remaining, 1)} runtime hours remaining before service.")

        # --- ADVANCED SPECIFIC FUEL CONSUMPTION (SFC) MATHEMATICAL ENGINES ---
        kw_input = request.form.get(f'{gen_id}_kw')
        if kw_input and kw_input.strip() != "":
            try:
                current_kwhr_val = float(kw_input)
                if prev_log and prev_log.kw and str(prev_log.kw).strip() != "":
                    prev_kwhr_val = float(prev_log.kw)
                    delta_kwhr = current_kwhr_val - prev_kwhr_val
                    
                    if delta_kwhr < 0:
                        active_maintenance_alerts.append(
                            f"❌ DATA ANOMALY: Unit AG-{clean_num} current kWhr counter ({current_kwhr_val}) is lower than the previous recorded shift counter ({prev_kwhr_val}). Check for odometer rollover or entry typo!"
                        )
                    elif delta_kwhr > 0:
                        total_gen_gallons = 0.0
                        fuel_idx = 1
                        while True:
                            t_val = request.form.get(f'fuel_time_{fuel_idx}')
                            if not t_val: break
                            gal_str = request.form.get(f'fuel_ag{clean_num}_{fuel_idx}')
                            if gal_str and gal_str.strip() != "":
                                try:
                                    total_gen_gallons += float(gal_str)
                                except ValueError:
                                    pass
                            fuel_idx += 1
                            
                        total_gen_liters = total_gen_gallons * 3.78541
                        sfc_current = total_gen_liters / delta_kwhr
                        
                        if sfc_current > 0.45:
                            active_maintenance_alerts.append(
                                f"⚠️ HIGH SFC ANOMALY: Unit AG-{clean_num} has an elevated Specific Fuel Consumption (SFC) of {round(sfc_current, 4)} L/kWhr! Inspect for active structural fuel leaks, mechanical drag, or incorrect manual entries."
                            )
                        elif sfc_current < 0.15 and total_gen_gallons > 0:
                            active_maintenance_alerts.append(
                                f"⚠️ LOW SFC ANOMALY: Unit AG-{clean_num} has an abnormally low Specific Fuel Consumption (SFC) of {round(sfc_current, 4)} L/kWhr! Verify if some refueling volumes were left unrecorded."
                            )
            except ValueError:
                pass

    # 🔄 3. COMMIT SHIFT DATA ENTRIES TO SQLITE
    try:
        db.session.commit()
        
        # 🛡️ THE SECURITY CLEANUP FIX: 
        # Log the alerts safely to the server backend console instead of using flash() 
        # so they stay away from the operator interface view completely!
        if active_maintenance_alerts:
            for alert in active_maintenance_alerts:
                print(f"[ENGINEERING AUDIT LOG] {alert}")

        flash("Shift report submitted successfully!")
        
    except Exception as e:
        db.session.rollback()
        flash(f"🚨 Critical database write fault: {str(e)}")

    return redirect(url_for('operator_dashboard', shift_type=chosen_shift))



# --- SUPERVISOR SECTION ---
@app.route('/supervisor', methods=['GET', 'POST'])
@login_required
def supervisor_dashboard():
    # ... keep your login role validation checks here ...

   # 🌟 EXTRACT: Query parameters from the dashboard search controls
    start_date_str = request.args.get('start_date', '')
    active_tab = request.args.get('active_tab', '') # 'day' or 'night'
    
    # 🏎️ NEW STRATEGY: Intercept search queries and auto-route to the operator entry layout
    if start_date_str and active_tab:
        try:
            from datetime import datetime
            search_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            shift_name = 'Day' if active_tab == 'day' else 'Night'
            
            # Query the database for this specific shift report document
            target_report = ShiftReport.query.filter(
                db.func.date(ShiftReport.timestamp) == search_date,
                ShiftReport.shift_type == shift_name
            ).first()
            
            if target_report:
                # Success! Forward the supervisor straight to the operator edit/review page
                return redirect(url_for('review_shift', report_id=target_report.id))
            else:
                # Fallback warning if an operator hasn't submitted a report for that day yet
                flash(f"No operational {shift_name} shift report was found for {start_date_str}.", "warning")
                
        except Exception as e:
            print(f"Error executing auto-routing sequence: {e}")

    # ====================================================================
    # 📊 PRESERVED: CHRONOLOGICAL DELTA METRICS ENGINE FOR DISPLAY CARDS
    # ====================================================================
    from datetime import datetime as dt, timedelta
    weekly_window_date = dt.now() - timedelta(days=7)
    all_fuel_logs = FuelLog.query.all()
    
    total_gallons = 0.0
    for f_log in all_fuel_logs:
        log_date = None
        if hasattr(f_log, 'report') and f_log.report and hasattr(f_log.report, 'timestamp'):
            log_date = f_log.report.timestamp
        elif hasattr(f_log, 'timestamp') and f_log.timestamp:
            log_date = f_log.timestamp

        # Fuel efficiency calculations strictly keep the 7-day window filter
        if log_date is None or log_date >= weekly_window_date:
            if f_log.gallons_consumed and f_log.gallons_consumed.strip() != "":
                try: total_gallons += float(f_log.gallons_consumed)
                except ValueError: continue

    total_kwh = 0.0
    distinct_gensets = db.session.query(GeneratorLog.genset_id).distinct().all()
    genset_ids = [g[0] for g in distinct_gensets if g[0]]

    HARDCODED_BASELINES = {
        'ag 2': 1290.0, 'ag 4': 4472352.0, 'ag 5': 3448095.0,  
        'ag 7': 13837.0, 'ag 8': 1096792.0, 'ag 9': 1081938.0   
    }

    # 🚨 INDUSTRY STANDARD: Unrestricted Exception Queue Loop for Anomalies
    active_maintenance_alerts = []
    
    for g_id in genset_ids:
        all_logs_for_unit = GeneratorLog.query.filter_by(genset_id=g_id).order_by(GeneratorLog.id.asc()).all()
        clean_num = str(g_id).lower().replace('ag', '').strip()
        
        for i in range(0, len(all_logs_for_unit)):
            current_log = all_logs_for_unit[i]
            if i == 0: continue
            previous_log = all_logs_for_unit[i - 1]

            # 🛠️ THE FIXED LINK: Safely lookup the ShiftReport via its ID to get around the missing relationship property
            parent_report = None
            if current_log.report_id:
                parent_report = db.session.get(ShiftReport, current_log.report_id) if hasattr(db.session, 'get') else ShiftReport.query.get(current_log.report_id)

            # Calculate parameters safely
            log_date = parent_report.timestamp if parent_report else None
            shift_name_str = parent_report.shift_type.lower() if (parent_report and parent_report.shift_type) else 'day'
            
            if current_log.kw:
                try:
                    curr_val = float(current_log.kw.strip())
                    if i - 1 == 0 and g_id in HARDCODED_BASELINES:
                        prev_val = HARDCODED_BASELINES[g_id]
                    else:
                        prev_val = float(previous_log.kw.strip()) if previous_log.kw else curr_val
                    
                    shift_delta = curr_val - prev_val
                    
                    # 🔥 FIXED: Removed the weekly window filter completely so ALL your historical kWh pulls through!
                    if shift_delta >= 0: 
                        total_kwh += shift_delta

                    # 1. Delta Counter Check (Typo rollbacks)
                    if shift_delta < 0:
                       active_maintenance_alerts.append({
                        'genset_id': g_id,
                        'report_id': current_log.report_id,
                        'shift_type': shift_name_str,
                        'date': log_date.strftime('%b %d, %Y') if log_date else 'Unknown Date',  # 🌟 ADD THIS LINE
                        'message': f"❌ DATA ANOMALY: Unit AG-{clean_num} current kWhr counter..."
                    })

                    # Calculate log consumption for SFC checks
                    total_gen_gallons = 0.0
                    if parent_report and hasattr(parent_report, 'fuel_logs') and parent_report.fuel_logs:
                        for fl in parent_report.fuel_logs:
                            if fl.genset_id == g_id and fl.gallons_consumed:
                                try: total_gen_gallons += float(fl.gallons_consumed)
                                except ValueError: pass

                    sfc_current = (total_gen_gallons / shift_delta) if shift_delta > 0 else 0.0
                    
                    # 2. SFC Out of Bounds Rules
                    if sfc_current > 0.45:
                        active_maintenance_alerts.append({
                            'genset_id': g_id,
                            'report_id': current_log.report_id,
                            'shift_type': shift_name_str,
                            'message': f"⚠️ HIGH SFC ANOMALY: Unit AG-{clean_num} has an elevated Specific Fuel Consumption (SFC) of {round(sfc_current, 4)} L/kWhr! Inspect for active structural fuel leaks, mechanical drag, or incorrect manual entries."
                        })
                    elif sfc_current < 0.15 and total_gen_gallons > 0:
                        active_maintenance_alerts.append({
                            'genset_id': g_id,
                            'report_id': current_log.report_id,
                            'shift_type': shift_name_str,
                            'message': f"⚠️ LOW SFC ANOMALY: Unit AG-{clean_num} has an abnormally low Specific Fuel Consumption (SFC) of {round(sfc_current, 4)} L/kWhr! Verify if some refueling volumes were left unrecorded."
                        })
                        
                except ValueError: 
                    continue

            # 3. Runtime Maintenance Countdown checks
            if current_log.run_hours:
                try:
                    hours_val = float(current_log.run_hours)
                    remaining = 250 - (hours_val % 250)
                    if remaining <= 49:
                        active_maintenance_alerts.append({
                            'genset_id': g_id,
                            'report_id': current_log.report_id,
                            'shift_type': shift_name_str,
                            'message': f"⚠️ CRITICAL: Unit AG-{clean_num} has only {round(remaining, 1)} runtime hours remaining before service!"
                        })
                    elif remaining <= 99:
                        active_maintenance_alerts.append({
                            'genset_id': g_id,
                            'report_id': current_log.report_id,
                            'shift_type': shift_name_str,
                            'message': f"🚨 ALERT: Unit AG-{clean_num} has only {round(remaining, 1)} runtime hours remaining before service."
                        })
                except ValueError:
                    pass

    # 🌟 FIXED: sfc_metric is now unindented completely out of the loops! 
    sfc_metric = 0.00
    if total_kwh > 0:
        sfc_metric = round(total_gallons / total_kwh, 3)

    # 🔄 APPROACH A MATCHING COUNT LINKAGE: Force card count to match anomaly list length exactly
    active_faults_count = len(active_maintenance_alerts)

    system_health = "Optimal"
    if active_faults_count > 3:
        system_health = "Critical"
    elif active_faults_count > 0:
        system_health = "Warning"

    reports = ShiftReport.query.order_by(ShiftReport.timestamp.desc()).limit(10).all()
    maintenance_tasks = MaintenanceTask.query.all()
    
    return render_template('supervisor.html',
                           current_user=current_user,
                           total_kwh=round(total_kwh, 1),
                           total_gallons=round(total_gallons, 1),
                           sfc_metric=sfc_metric,
                           active_faults_count=active_faults_count,
                           system_health=system_health,
                           reports=reports,
                           maintenance_tasks=maintenance_tasks,
                           tasks=maintenance_tasks,
                           active_maintenance_alerts=active_maintenance_alerts, # Array pushed down safely
                           start_date=start_date_str,
                           end_date='',
                           day_report=None,
                           night_report=None,
                           search_triggered=False,
                           active_tab=active_tab)
# --- ADMIN SECTION ---
@app.route('/admin', methods=['GET', 'POST'])
@login_required
def admin_dashboard():
    # 🛡️ Access Control Security Check
    if current_user.role != 'super_admin':
        return "Access Denied. Super Admins Only.", 403
        
    # Ensure System Settings baseline is populated
    system_settings = SystemSettings.query.first()
    if not system_settings:
        system_settings = SystemSettings(company_name="Powerhouse Enterprise", logo_path="default_logo.png")
        db.session.add(system_settings)
        db.session.commit()

    # Query the list of system users to populate your active directory management table
    all_users = User.query.all()
    
    return render_template('admin.html', 
                           settings=system_settings, 
                           users=all_users, 
                           current_user=current_user)

@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    from flask import session; session.pop('_flashes', None)

    # 🛡️ Aligned Security Check
    if current_user.role != 'super_admin':
        flash("Unauthorized action. Super Admins Only.")
        return redirect('/')

    # 🔐 Self-Deletion Guardrail
    if current_user.id == user_id:
        flash("❌ Operational error: You cannot delete your own active account.")
        return redirect('/admin') # 👈 Hardcoded direct string URL route path

    # Find the user matching the structural class name 'User'
    user_to_delete = User.query.get(user_id)
    if not user_to_delete:
        flash("❌ User not found.")
        return redirect('/admin')

    try:
        db.session.delete(user_to_delete)
        db.session.commit()
        flash(f"✅ Account for {user_to_delete.username} has been successfully deleted!")
    except Exception as e:
        db.session.rollback()
        flash(f"🚨 Database error during deletion: {str(e)}")

    return redirect('/admin') # 👈 Hardcoded direct string URL route path


@app.route('/admin/reset_password/<int:user_id>')
@login_required
def reset_password(user_id):
    from flask import session; session.pop('_flashes', None)

    # 🛡️ Aligned Security Check
    if current_user.role != 'super_admin':
        flash("Unauthorized action. Super Admins Only.")
        return redirect('/')

    user_to_reset = User.query.get(user_id)
    if not user_to_reset:
        flash("❌ User not found.")
        return redirect('/admin')

    try:
        user_to_reset.password_hash = generate_password_hash("Reset123!")
        user_to_reset.must_change_password = True
        db.session.commit()
        flash(f"✅ Password for {user_to_reset.username} has been reset to: Reset123!")
    except Exception as e:
        db.session.rollback()
        flash(f"🚨 Database error during reset: {str(e)}")

    return redirect('/admin')

@app.route('/supervisor/update_logs', methods=['POST'])
def update_logs():
    redirect_date = request.form.get('redirect_date', '')
    day_report_id = request.form.get('day_report_id')
    night_report_id = request.form.get('night_report_id')
    
    for key, value in request.form.items():
        if not value.strip(): 
            continue
            
        # 1. Direct Run Hours Correction Override
        if '_hours_' in key:
            parts = key.split('_hours_')
            shift_type = parts[0]   
            g_id = parts[1]         
            report_id = day_report_id if shift_type == 'day' else night_report_id
            if report_id:
                log = GeneratorLog.query.filter_by(report_id=report_id, genset_id=g_id).first()
                if log:
                    try:
                        log.run_hours = float(value)
                        db.session.commit()
                    except ValueError: pass

        # 2. Direct kWh Counter Correction Override
        elif '_kw_' in key:
            parts = key.split('_kw_')
            shift_type = parts[0]   
            g_id = parts[1]         
            report_id = day_report_id if shift_type == 'day' else night_report_id
            if report_id:
                log = GeneratorLog.query.filter_by(report_id=report_id, genset_id=g_id).first()
                if log:
                    try:
                        log.kw = str(int(float(value)))  
                        db.session.commit()
                    except ValueError: pass

        # ⛽ 3. INDUSTRY STANDARD: Direct Fuel Consumption Total Value Overwrite
        elif '_fuel_total_' in key:
            parts = key.split('_fuel_total_')
            shift_type = parts[0]
            g_id = parts[1]
            report_id = day_report_id if shift_type == 'day' else night_report_id
            
            if report_id:
                f_log = FuelLog.query.filter_by(report_id=report_id, genset_id=g_id).first()
                if f_log:
                    try:
                        # Replaces the historical entry directly with the verified actual value
                        f_log.gallons_consumed = float(value)
                        db.session.commit()
                    except ValueError: pass

    return redirect(url_for('supervisor_dashboard', start_date=redirect_date))

@app.route('/update_branding', methods=['POST'])
@login_required
def update_branding():
    if current_user.role != 'super_admin':
        return "Access Denied. Super Admins Only.", 403

    system_settings = SystemSettings.query.first()
    if not system_settings:
        system_settings = SystemSettings(company_name="Powerhouse Enterprise", logo_path="default_logo.png")
        db.session.add(system_settings)

    new_name = request.form.get('company_name')
    if new_name:
        system_settings.company_name = new_name

    if 'logo_file' in request.files:
        file = request.files['logo_file']
        if file and file.filename != '':
            ext = os.path.splitext(secure_filename(file.filename))[1]
            saved_filename = f"custom_logo{ext}"
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], saved_filename))
            system_settings.logo_path = saved_filename

    db.session.commit()
    flash("System branding updated successfully!")
    return redirect(url_for('admin_dashboard'))


@app.route('/add_user', methods=['POST'])
@login_required
def add_user():
    from flask import session; session.pop('_flashes', None)

    # 🛡️ ONLY block users who are NOT admins. If they ARE an admin, let them pass!
    if current_user.role != 'super_admin' and current_user.role != 'supervisor':
        flash("Unauthorized action.")
        return redirect(url_for('login'))

    # Grab the data from your HTML form fields
    username = request.form.get('new_username')
    password = request.form.get('new_password')
    role = request.form.get('new_role')

    # Double check we actually got data so we don't save blank rows
    if not username or not password:
        flash("❌ Username and password are required.")
        return redirect(request.referrer or url_for('admin_dashboard'))

    # Check if the username is already taken
    existing_user = User.query.filter_by(username=username).first()
    if existing_user:
        flash("❌ Username already exists!")
        return redirect(request.referrer or url_for('admin_dashboard'))

    try:
        # Create and save the user safely
        new_user = User(
            username=username,
            password_hash=generate_password_hash(password), 
            role=role,
            must_change_password=True # 👈 The single clean update added here!
        )
        db.session.add(new_user)
        db.session.commit()

        flash(f"✅ Account created successfully for {username} as {role}!")
    except Exception as e:
        db.session.rollback()
        flash(f"🚨 Database error: {str(e)}")

    # Go right back to where you came from
    return redirect(request.referrer or url_for('admin_dashboard'))


# --- SHIFT REVIEW & VERIFICATION HELPER METRICS ---
def get_delta_kwhr(log):
    if not log.kw: 
        return 0.0
    try:
        current_val = float(log.kw)
    except ValueError:
        return 0.0
    clean_num = log.genset_id.upper().replace('AG', '').replace('-', '').strip()
    possible_ids = [f"AG{clean_num}", f"ag{clean_num}", f"AG-{clean_num}", f"ag-{clean_num}"]
    prev_log = GeneratorLog.query.filter(
        GeneratorLog.genset_id.in_(possible_ids), 
        GeneratorLog.id < log.id
    ).order_by(GeneratorLog.id.desc()).first()
    if prev_log and prev_log.kw:
        try:
            return max(0.0, current_val - float(prev_log.kw))
        except ValueError:
            return 0.0
    return 0.0


def get_fuel_liters(log):
    # BULLETPROOF CLEANING: Strip 'AG', 'ag', and '-' to get just the raw number
    clean_num = log.genset_id.upper().replace('AG', '').replace('-', '').strip()
    possible_ids = [f"AG{clean_num}", f"ag{clean_num}", f"AG-{clean_num}", f"ag-{clean_num}"]
    
    fuel_entries = FuelLog.query.filter(
        FuelLog.report_id == log.report_id, 
        FuelLog.genset_id.in_(possible_ids)
    ).all()
    
    total_gallons = 0.0
    for entry in fuel_entries:
        if entry.gallons_consumed:
            try:
                total_gallons += float(entry.gallons_consumed)
            except ValueError:
                pass
    return total_gallons * 3.78541




# --- SHIFT REVIEW & VERIFICATION ---
@app.route('/review_shift/<int:report_id>', methods=['GET', 'POST'])
@login_required
def review_shift(report_id):
    if current_user.role != 'supervisor' and current_user.role != 'super_admin':
        flash("Unauthorized access.")
        return redirect(url_for('login'))
        
    report = ShiftReport.query.get_or_404(report_id)
    generator_logs = GeneratorLog.query.filter_by(report_id=report_id).all()
    reefer_inventory = ReeferInventory.query.filter_by(report_id=report_id).first()
    reefer_faults = ReeferFault.query.filter_by(report_id=report_id).all()
    fuel_logs = FuelLog.query.filter_by(report_id=report_id).all()
    
    if request.method == 'POST':
        # Save Report Meta
        report.status = request.form.get('status')
        
        # Save every single line item raw from the form submissions
        for gen in generator_logs:
            gen.volts = request.form.get(f'volts_{gen.id}')
            gen.amps = request.form.get(f'amps_{gen.id}')
            gen.load_pct = request.form.get(f'load_pct_{gen.id}')
            gen.kw = request.form.get(f'kw_{gen.id}')
            gen.battery_v = request.form.get(f'battery_v_{gen.id}')
            gen.temp_c = request.form.get(f'temp_c_{gen.id}')
            gen.run_hours = request.form.get(f'hours_{gen.id}')
            gen.next_service = request.form.get(f'next_service_{gen.id}')

        for ref in reefer_faults:
            ref.temperature = request.form.get(f'reefer_temp_{ref.id}')
            ref.status = request.form.get(f'reefer_status_{ref.id}')

        for fuel in fuel_logs:
            fuel.gallons_consumed = request.form.get(f'fuel_consumed_{fuel.id}')
            fuel.gallons_added = request.form.get(f'fuel_added_{fuel.id}')

        db.session.commit()
        flash(f"✅ Shift report #{report_id} completely updated successfully!")
        
        if current_user.role == 'super_admin':
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('supervisor_dashboard'))
        
    return render_template(
        'review_shift.html', 
        report=report, 
        generator_logs=generator_logs,
        fuel_logs=fuel_logs, 
        current_user=current_user,
        reefer_inventory=reefer_inventory,
        reefer_faults=reefer_faults
    )
    # Place this inside app.py where your initialization logic resides
    with app.app_context():
    # This ensures your tables match your models.py definitions (255 chars)
    db.create_all()
    print("Database schema synchronized with models.py", flush=True)
    # Force port binding for Render (defaulting to 10000 if not specified)
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
