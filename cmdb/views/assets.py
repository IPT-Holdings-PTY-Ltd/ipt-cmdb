"""Asset (Configuration Item) views."""

from flask import Blueprint, render_template, redirect, url_for, flash
from ..extensions import db
from ..models import Asset
from ..forms import AssetForm

assets_bp = Blueprint("assets", __name__, url_prefix="/assets")


@assets_bp.route("/")
def index():
    assets = Asset.query.order_by(Asset.name).all()
    return render_template("assets/index.html", assets=assets)


@assets_bp.route("/new", methods=["GET", "POST"])
def create():
    form = AssetForm()
    if form.validate_on_submit():
        asset = Asset(
            name=form.name.data,
            asset_type=form.asset_type.data,
            description=form.description.data,
            status=form.status.data,
            environment=form.environment.data,
            owner=form.owner.data,
            location=form.location.data,
            ip_address=form.ip_address.data,
        )
        db.session.add(asset)
        db.session.commit()
        flash(f"Asset '{asset.name}' created.", "success")
        return redirect(url_for("assets.detail", asset_id=asset.id))
    return render_template("assets/form.html", form=form, title="New Asset")


@assets_bp.route("/<int:asset_id>")
def detail(asset_id):
    asset = db.get_or_404(Asset, asset_id)
    return render_template("assets/detail.html", asset=asset)


@assets_bp.route("/<int:asset_id>/edit", methods=["GET", "POST"])
def edit(asset_id):
    asset = db.get_or_404(Asset, asset_id)
    form = AssetForm(obj=asset)
    if form.validate_on_submit():
        form.populate_obj(asset)
        db.session.commit()
        flash(f"Asset '{asset.name}' updated.", "success")
        return redirect(url_for("assets.detail", asset_id=asset.id))
    return render_template("assets/form.html", form=form, title="Edit Asset", asset=asset)


@assets_bp.route("/<int:asset_id>/delete", methods=["POST"])
def delete(asset_id):
    asset = db.get_or_404(Asset, asset_id)
    name = asset.name
    db.session.delete(asset)
    db.session.commit()
    flash(f"Asset '{name}' deleted.", "warning")
    return redirect(url_for("assets.index"))
