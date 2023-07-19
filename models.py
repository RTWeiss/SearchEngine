from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class IndexedURL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(500), nullable=False)
    title = db.Column(db.String(500), nullable=True)  # Added title column
    description = db.Column(db.Text, nullable=True)  # Added description column
    type = db.Column(db.String(50), nullable=True)   # Added type column

    def __init__(self, url, title, description, type):
        self.url = url
        self.title = title
        self.description = description
        self.type = type
