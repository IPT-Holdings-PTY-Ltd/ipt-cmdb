"""Database models for IPT-CMDB."""

from datetime import datetime, timezone
from .extensions import db


class Asset(db.Model):
    """A Configuration Item (CI) – any resource tracked in the CMDB."""

    __tablename__ = "assets"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    asset_type = db.Column(db.String(60), nullable=False)  # server, app, network, service, db, other
    description = db.Column(db.Text, default="")
    status = db.Column(db.String(30), nullable=False, default="active")  # active, inactive, decommissioned
    environment = db.Column(db.String(30), nullable=False, default="production")  # production, staging, development
    owner = db.Column(db.String(120), default="")
    location = db.Column(db.String(120), default="")
    ip_address = db.Column(db.String(45), default="")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships where this asset is the *source* (it depends on others)
    outgoing = db.relationship(
        "Relationship",
        foreign_keys="Relationship.source_id",
        back_populates="source",
        cascade="all, delete-orphan",
    )
    # Relationships where this asset is the *target* (others depend on it)
    incoming = db.relationship(
        "Relationship",
        foreign_keys="Relationship.target_id",
        back_populates="target",
        cascade="all, delete-orphan",
    )

    change_records = db.relationship("ChangeRecord", back_populates="asset", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Asset {self.name!r}>"


class Relationship(db.Model):
    """A directed relationship between two assets."""

    __tablename__ = "relationships"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey("assets.id"), nullable=False)
    target_id = db.Column(db.Integer, db.ForeignKey("assets.id"), nullable=False)
    rel_type = db.Column(db.String(60), nullable=False, default="depends_on")  # depends_on, connected_to, hosted_on, part_of
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    source = db.relationship("Asset", foreign_keys=[source_id], back_populates="outgoing")
    target = db.relationship("Asset", foreign_keys=[target_id], back_populates="incoming")

    __table_args__ = (
        db.UniqueConstraint("source_id", "target_id", "rel_type", name="uq_relationship"),
    )

    def __repr__(self):
        return f"<Relationship {self.source_id} -{self.rel_type}-> {self.target_id}>"


# Association table: a change record can directly impact many assets
change_impact = db.Table(
    "change_impact",
    db.Column("change_id", db.Integer, db.ForeignKey("change_records.id"), primary_key=True),
    db.Column("asset_id", db.Integer, db.ForeignKey("assets.id"), primary_key=True),
)


class ChangeRecord(db.Model):
    """A record of a change made to a primary asset, with optional impacted assets."""

    __tablename__ = "change_records"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    asset_id = db.Column(db.Integer, db.ForeignKey("assets.id"), nullable=False)
    change_type = db.Column(db.String(30), nullable=False, default="modification")  # addition, modification, removal
    description = db.Column(db.Text, default="")
    changed_by = db.Column(db.String(120), default="")
    change_date = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    status = db.Column(db.String(30), nullable=False, default="planned")  # planned, in_progress, completed, failed
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    asset = db.relationship("Asset", back_populates="change_records")
    impacted_assets = db.relationship("Asset", secondary=change_impact, backref="impacting_changes")

    def __repr__(self):
        return f"<ChangeRecord {self.title!r}>"
