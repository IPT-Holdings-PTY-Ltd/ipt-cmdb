"""Relationship views."""

from flask import Blueprint, render_template, redirect, url_for, flash
from ..extensions import db
from ..models import Asset, Relationship
from ..forms import RelationshipForm

relationships_bp = Blueprint("relationships", __name__, url_prefix="/relationships")


def _populate_asset_choices(form):
    assets = Asset.query.order_by(Asset.name).all()
    choices = [(a.id, a.name) for a in assets]
    form.source_id.choices = choices
    form.target_id.choices = choices


@relationships_bp.route("/")
def index():
    relationships = (
        Relationship.query
        .join(Asset, Relationship.source_id == Asset.id)
        .order_by(Asset.name)
        .all()
    )
    return render_template("relationships/index.html", relationships=relationships)


@relationships_bp.route("/new", methods=["GET", "POST"])
def create():
    form = RelationshipForm()
    _populate_asset_choices(form)
    if form.validate_on_submit():
        if form.source_id.data == form.target_id.data:
            flash("Source and target asset must be different.", "danger")
        else:
            rel = Relationship(
                source_id=form.source_id.data,
                target_id=form.target_id.data,
                rel_type=form.rel_type.data,
                notes=form.notes.data,
            )
            db.session.add(rel)
            db.session.commit()
            flash("Relationship created.", "success")
            return redirect(url_for("relationships.index"))
    return render_template("relationships/form.html", form=form, title="New Relationship")


@relationships_bp.route("/<int:rel_id>/delete", methods=["POST"])
def delete(rel_id):
    rel = db.get_or_404(Relationship, rel_id)
    db.session.delete(rel)
    db.session.commit()
    flash("Relationship deleted.", "warning")
    return redirect(url_for("relationships.index"))
