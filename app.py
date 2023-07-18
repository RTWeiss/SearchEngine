import os
import logging
import pickle
import time
from flask import Flask, render_template, request, redirect, url_for, flash
from bs4 import BeautifulSoup
import requests
import threading
from threading import Lock
import queue
import re
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import escape
from urllib.parse import urljoin, urlparse
import urllib.parse
import random
from models import db, IndexedURL
from datetime import datetime

DATABASE_URL = os.getenv('DATABASE_URL')

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app = Flask(__name__)
app.secret_key = 'your_secret_key'

lock = Lock()
INDEX = {}
SITEMAP_STATUS = {}

# Using environment variable for the database URL
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

class SearchQuery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    query = db.Column(db.String(500))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow) # Added timestamp column

class SubmittedSitemap(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(500), nullable=False)
    indexing_status = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(100), nullable=False)
    total_urls = db.Column(db.Integer, nullable=False)
    indexed_urls = db.Column(db.Integer, nullable=True)

with app.app_context():
    db.create_all()

SITEMAP_QUEUE = queue.Queue()
MAX_SIMULTANEOUS_INDEXING = 5
CURRENTLY_INDEXING = 0

# Set up logging
logging.basicConfig(filename='app.log', filemode='w', format='%(name)s - %(levelname)s - %(message)s', level=logging.DEBUG)

@app.route('/', methods=['GET', 'POST'])
def search():
    if request.method == 'POST':
        query = request.form.get('query').lower()
        new_query = SearchQuery(query=query)
        db.session.add(new_query)
        db.session.commit()

        # For every url in the database, check if the query is in the title, description, or url
        results = {
            url.url: {**pickle.loads(url.indexed_data), 'title': escape(pickle.loads(url.indexed_data)['title']), 'description': escape(pickle.loads(url.indexed_data)['description'])}
            for url in IndexedURL.query.all()
            if query in pickle.loads(url.indexed_data)['title'].lower() or query in url.url.lower() or query in pickle.loads(url.indexed_data)['description'].lower()
        }
        return render_template('results.html', query=query, results=results)
    return render_template('search.html')

def start_background_thread():
    while True:
        process_sitemap_queue()
        time.sleep(5)  # Sleep for 5 seconds between checks

def process_sitemap_queue():
    global CURRENTLY_INDEXING
    while not SITEMAP_QUEUE.empty():
        with lock:
            if CURRENTLY_INDEXING < MAX_SIMULTANEOUS_INDEXING:
                sitemap_url = SITEMAP_QUEUE.get()
                SITEMAP_STATUS[sitemap_url] = 'Waiting to start indexing...'
                indexing_thread = threading.Thread(target=index_sitemap, args=(sitemap_url,))
                indexing_thread.start()
                CURRENTLY_INDEXING += 1
                logging.info(f'Started indexing thread for: {sitemap_url}')

def index_sitemap(sitemap_url):
    global CURRENTLY_INDEXING, INDEX

    SITEMAP_STATUS[sitemap_url] = {'status': 'Indexing started...'}
    urls = get_urls_from_sitemap(sitemap_url)
    SITEMAP_STATUS[sitemap_url]['total_urls'] = len(urls)
    logging.info(f'Found {len(urls)} URLs in sitemap: {sitemap_url}')

    for url in urls:
        if sitemap_url not in SITEMAP_STATUS:
            logging.info(f"Stopped indexing for deleted sitemap: {sitemap_url}")
            return
        else:
            try:
                index_url(url)
            except Exception as e:
                logging.error(f"Error occurred while indexing URL {url}: {e}", exc_info=True)

    CURRENTLY_INDEXING -= 1
    SITEMAP_STATUS[sitemap_url]['status'] = 'Indexing finished'

    process_sitemap_queue()

def get_urls_from_sitemap(sitemap_url):
    response = requests.get(sitemap_url)
    soup = BeautifulSoup(response.text, "xml")
    urls = [loc.text for loc in soup.find_all("loc")]
    return urls

def index_url(url):
    try:
        res = requests.get(url)
        page_soup = BeautifulSoup(res.text, "html.parser")
        title = page_soup.find("title").text if page_soup.find("title") else url
        description = page_soup.find("meta", attrs={"name": "description"})
        description = description["content"] if description else "No description available"
        INDEX[url] = {"title": title, "description": description}
        logging.info(f'Successfully indexed URL: {url}')

        # Save the indexed data to the database
        new_indexed_url = IndexedURL(url=url, title=title, description=description)
        db.session.add(new_indexed_url)
        db.session.commit()

        # Update the SubmittedSitemap record
        sitemap = SubmittedSitemap.query.filter(SubmittedSitemap.url.contains(url)).first()
        if sitemap:
            sitemap.indexed_urls += 1
            db.session.commit()
    except Exception as e:
        logging.error(f"Failed to index URL: {url}", exc_info=True)

@app.route("/submit", methods=["GET", "POST"])
def submit():
    if request.method == "POST":
        sitemap_url = request.form["sitemap_url"]
        new_sitemap = SubmittedSitemap(url=sitemap_url, indexing_status='Added to queue', status='Not started', total_urls=0, indexed_urls=0)
        db.session.add(new_sitemap)
        db.session.commit()
        SITEMAP_QUEUE.put(sitemap_url)  # Add the submitted sitemap to the queue
        flash("Sitemap submitted successfully.")
        return redirect(url_for('submit'))  # Redirect back to the same page
    else:
        return render_template("submit.html")  # Render the submit page if the request method is GET

@app.route("/dashboard", methods=["GET"])
def dashboard():
    try:
        submitted_sitemaps = SubmittedSitemap.query.all()
        sitemap_status = {
            sitemap.url: {
                'status': sitemap.status,
                'total_urls': sitemap.total_urls,
                'indexed_urls': sitemap.indexed_urls,
            }
            for sitemap in submitted_sitemaps
        }
        return render_template("dashboard.html", sitemap_status=sitemap_status)
    except Exception as e:
        logging.error(f"An error occurred while loading the dashboard: {e}", exc_info=True)
        return str(e), 500  # return the exception message for debugging

@app.route("/urls", methods=["GET"])
def urls():
    return render_template("urls.html", index=INDEX)

@app.route('/all_search_queries', methods=['GET'])
def all_search_queries():
    # Retrieving all search queries from the database
    queries = SearchQuery.query.order_by(SearchQuery.id.desc()).all()
    return render_template('all_search_queries.html', queries=queries)

@app.route('/delete_sitemap')
def delete_sitemap():
    sitemap_url = request.args.get('sitemap_url')
    if sitemap_url is None: # Added check for sitemap_url
        flash('Invalid Sitemap URL')
        return redirect(url_for('dashboard'))
    sitemap_url = urllib.parse.unquote(sitemap_url)
    sitemap = SubmittedSitemap.query.filter_by(url=sitemap_url).first()
    if sitemap:
        db.session.delete(sitemap)
        db.session.commit()
        flash('Sitemap has been deleted successfully')
    else:
        flash('Sitemap URL not found')
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    thread = threading.Thread(target=start_background_thread)
    thread.start()
    app.run(debug=True, threaded=True)

