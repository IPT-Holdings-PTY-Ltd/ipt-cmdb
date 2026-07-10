"""Change record views."""

from datetime import datetime, timezone
from flask import Blueprint, render_template, redirect, url_for, flash
from ..extensions import db
from ..models import Asset, ChangeRecord
from ..forms import ChangeRecordForm

changes_bp = Blueprint("changes", __name__, url_prefix="/changes")


def _populate_choices(form):
    assets = Asset.query.order_by(Asset.name).all()
    choices = [(a.id, a.name) for a in assets]
    form.asset_id.choices = choices
    form.impacted_asset_ids.choices = choices


@changes_bp.route("/")
def index():
    records = ChangeRecord.query.order_by(ChangeRecord.change_date.desc()).all()
    return render_template("changes/index.html", records=records)


@changes_bp.route("/new", methods=["GET", "POST"])
def create():
    form = ChangeRecordForm()
    _populate_choices(form)
    if not form.change_date.data:
        form.change_date.data = datetime.now(timezone.utc).replace(tzinfo=None)
    if form.validate_on_submit():
        record = ChangeRecord(
            title=form.title.data,
            asset_id=form.asset_id.data,
            change_type=form.change_type.data,
            description=form.description.data,
            changed_by=form.changed_by.data,
            change_date=form.change_date.data,
            status=form.status.data,
        )
        if form.impacted_asset_ids.data:
            record.impacted_assets = Asset.query.filter(Asset.id.in_(form.impacted_asset_ids.data)).all()
        db.session.add(record)
        db.session.commit()
        flash(f"Change record '{record.title}' created.", "success")
        return redirect(url_for("changes.detail", record_id=record.id))
    return render_template("changes/form.html", form=form, title="New Change Record")


@changes_bp.route("/<int:record_id>")
def detail(record_id):
    record = db.get_or_404(ChangeRecord, record_id)
    return render_template("changes/detail.html", record=record)


@changes_bp.route("/<int:record_id>/edit", methods=["GET", "POST"])
def edit(record_id):
    record = db.get_or_404(ChangeRecord, record_id)
    form = ChangeRecordForm(obj=record)
    _populate_choices(form)
    if form.validate_on_submit():
        record.title = form.title.data
        record.asset_id = form.asset_id.data
        record.change_type = form.change_type.data
        record.description = form.description.data
        record.changed_by = form.changed_by.data
        record.change_date = form.change_date.data
        record.status = form.status.data
        record.impacted_assets = (
            Asset.query.filter(Asset.id.in_(form.impacted_asset_ids.data)).all()
            if form.impacted_asset_ids.data
            else []
        )
        db.session.commit()
        flash(f"Change record '{record.title}' updated.", "success")
        return redirect(url_for("changes.detail", record_id=record.id))
    else:
        form.impacted_asset_ids.data = [a.id for a in record.impacted_assets]
    return render_template("changes/form.html", form=form, title="Edit Change Record", record=record)


@changes_bp.route("/<int:record_id>/delete", methods=["POST"])
def delete(record_id):
    record = db.get_or_404(ChangeRecord, record_id)
    title = record.title
    db.session.delete(record)
    db.session.commit()
    flash(f"Change record '{title}' deleted.", "warning")
    return redirect(url_for("changes.index"))
