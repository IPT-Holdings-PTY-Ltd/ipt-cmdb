from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

# Extensions are instantiated here without an app so they can be imported
# independently and initialised via their init_app() methods in create_app().
db = SQLAlchemy()
csrf = CSRFProtect()
