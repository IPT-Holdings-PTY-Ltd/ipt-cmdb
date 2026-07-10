"""
IPT-CMDB - A simple CMDB management application for MSPs.
Tracks configuration items, relationships, and changes.
"""

from flask import Flask
from .extensions import db, csrf
from .models import Asset, Relationship, ChangeRecord  # noqa: F401 – ensure models are registered


def create_app(config=None):
    app = Flask(__name__)

    app.config.setdefault("SECRET_KEY", "change-me-in-production")
    app.config.setdefault("SQLALCHEMY_DATABASE_URI", "sqlite:///cmdb.db")
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)

    if config:
        app.config.update(config)

    db.init_app(app)
    csrf.init_app(app)

    from .views.assets import assets_bp
    from .views.relationships import relationships_bp
    from .views.changes import changes_bp
    from .views.impact import impact_bp

    app.register_blueprint(assets_bp)
    app.register_blueprint(relationships_bp)
    app.register_blueprint(changes_bp)
    app.register_blueprint(impact_bp)

    with app.app_context():
        db.create_all()

    return app
