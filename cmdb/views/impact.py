"""Impact analysis view – given an asset, show what would be affected."""

from flask import Blueprint, render_template, request
from ..models import Asset, Relationship
from ..extensions import db

impact_bp = Blueprint("impact", __name__, url_prefix="/impact")


def _get_impacted(asset_id: int, visited: set | None = None) -> list[Asset]:
    """
    Recursively walk the dependency graph upstream:
    if `asset_id` changes, all assets that *depend on* it are impacted.
    Returns a list of unique impacted Asset objects (excluding the seed itself).
    """
    if visited is None:
        visited = set()
    if asset_id in visited:
        return []
    visited.add(asset_id)

    # Assets whose source depends on this target
    dependents = Relationship.query.filter_by(target_id=asset_id).all()
    result = []
    for rel in dependents:
        dep_asset = db.session.get(Asset, rel.source_id)
        if dep_asset and dep_asset.id not in visited:
            result.append(dep_asset)
            result.extend(_get_impacted(dep_asset.id, visited))
    return result


@impact_bp.route("/")
def index():
    assets = Asset.query.order_by(Asset.name).all()
    selected_id = request.args.get("asset_id", type=int)
    impacted = []
    selected_asset = None

    if selected_id:
        selected_asset = db.session.get(Asset, selected_id)
        if selected_asset:
            impacted = _get_impacted(selected_id)

    return render_template(
        "impact/index.html",
        assets=assets,
        selected_asset=selected_asset,
        impacted=impacted,
    )
