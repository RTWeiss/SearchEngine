from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class IndexedURL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(500), unique=True)
    title = db.Column(db.Text)
    description = db.Column(db.Text)
    type = db.Column(db.String(20))

    def __init__(self, url, title, description, type):
        self.url = url
        self.title = title
        self.description = description
        self.type = type

