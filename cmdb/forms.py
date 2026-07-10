"""WTForms form definitions for IPT-CMDB."""

from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, TextAreaField, DateTimeLocalField, SelectMultipleField
from wtforms.validators import DataRequired, Optional, Length


ASSET_TYPES = [
    ("server", "Server"),
    ("application", "Application"),
    ("network", "Network Device"),
    ("service", "Service"),
    ("database", "Database"),
    ("storage", "Storage"),
    ("other", "Other"),
]

ASSET_STATUSES = [
    ("active", "Active"),
    ("inactive", "Inactive"),
    ("decommissioned", "Decommissioned"),
]

ENVIRONMENTS = [
    ("production", "Production"),
    ("staging", "Staging"),
    ("development", "Development"),
]

RELATIONSHIP_TYPES = [
    ("depends_on", "Depends On"),
    ("connected_to", "Connected To"),
    ("hosted_on", "Hosted On"),
    ("part_of", "Part Of"),
]

CHANGE_TYPES = [
    ("addition", "Addition"),
    ("modification", "Modification"),
    ("removal", "Removal"),
]

CHANGE_STATUSES = [
    ("planned", "Planned"),
    ("in_progress", "In Progress"),
    ("completed", "Completed"),
    ("failed", "Failed"),
]


class AssetForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    asset_type = SelectField("Type", choices=ASSET_TYPES, validators=[DataRequired()])
    description = TextAreaField("Description", validators=[Optional()])
    status = SelectField("Status", choices=ASSET_STATUSES, default="active")
    environment = SelectField("Environment", choices=ENVIRONMENTS, default="production")
    owner = StringField("Owner", validators=[Optional(), Length(max=120)])
    location = StringField("Location", validators=[Optional(), Length(max=120)])
    ip_address = StringField("IP Address", validators=[Optional(), Length(max=45)])


class RelationshipForm(FlaskForm):
    source_id = SelectField("Source Asset", coerce=int, validators=[DataRequired()])
    target_id = SelectField("Target Asset", coerce=int, validators=[DataRequired()])
    rel_type = SelectField("Relationship Type", choices=RELATIONSHIP_TYPES, validators=[DataRequired()])
    notes = TextAreaField("Notes", validators=[Optional()])


class ChangeRecordForm(FlaskForm):
    title = StringField("Title", validators=[DataRequired(), Length(max=200)])
    asset_id = SelectField("Primary Asset", coerce=int, validators=[DataRequired()])
    change_type = SelectField("Change Type", choices=CHANGE_TYPES, default="modification")
    description = TextAreaField("Description", validators=[Optional()])
    changed_by = StringField("Changed By", validators=[Optional(), Length(max=120)])
    change_date = DateTimeLocalField("Change Date", format="%Y-%m-%dT%H:%M", validators=[DataRequired()])
    status = SelectField("Status", choices=CHANGE_STATUSES, default="planned")
    impacted_asset_ids = SelectMultipleField("Impacted Assets", coerce=int, validators=[Optional()])
