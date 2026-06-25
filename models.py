from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), default='operator')
    must_change_password = db.Column(db.Boolean, default=True, nullable=False)

class SystemSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(100))
    logo_path = db.Column(db.String(100))

class ShiftReport(db.Model):
    __tablename__ = 'shift_report'
    id = db.Column(db.Integer, primary_key=True)
    submitted_by = db.Column(db.String(80))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    # 🌟 NEW: Added shift_type column to safely differentiate Day and Night reports
    shift_type = db.Column(db.String(10), nullable=False, default='Day')

class ReeferInventory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey('shift_report.id'))
    start_count = db.Column(db.Integer)
    received = db.Column(db.Integer)
    delivered = db.Column(db.Integer)
    end_count = db.Column(db.Integer)

class ReeferFault(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey('shift_report.id'))
    reefer_id = db.Column(db.String(50))
    setpoint = db.Column(db.String(50))
    supply_temp = db.Column(db.String(50))
    return_temp = db.Column(db.String(50))
    alarm_code = db.Column(db.String(50))

class GeneratorLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey('shift_report.id'))
    genset_id = db.Column(db.String(20))
    volts = db.Column(db.String(20))
    amps = db.Column(db.String(20))
    load_pct = db.Column(db.String(20))
    kw = db.Column(db.String(20))
    battery_v = db.Column(db.String(20))
    temp_c = db.Column(db.String(20))
    run_hours = db.Column(db.String(20))
    # 🌟 FIXED FEATURE PRESERVED: Next Service threshold values column
    next_service = db.Column(db.String(20), nullable=True)

class FuelLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey('shift_report.id'))
    time_recorded = db.Column(db.String(20))
    genset_id = db.Column(db.String(20))
    gallons_consumed = db.Column(db.String(20))

class TaskLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey('shift_report.id'))
    task_type = db.Column(db.String(100))
    asset_id = db.Column(db.String(50))
    notes = db.Column(db.Text)
    progress_pct = db.Column(db.Integer)
    image_before = db.Column(db.String(200))
    # 🌟 FIXED FEATURE PRESERVED: Form matching "After Photo" column
    image_after = db.Column(db.String(200), nullable=True)

class MaintenanceTask(db.Model):
    __tablename__ = 'maintenance_task'
    
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.String(100))
    notes = db.Column(db.Text)
    progress = db.Column(db.Integer)
    status = db.Column(db.String(50), default='Active')
    task_type = db.Column(db.String(100))
    
    # 📸 ADD THESE TWO LINES SO PYTHON RECOGNIZES THE KEYWORDS!
    before_photo = db.Column(db.String(255), nullable=True)
    after_photo = db.Column(db.String(255), nullable=True)