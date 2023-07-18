import os
import logging
import pickle
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

app = Flask(__name__)
app.secret_key = 'your_secret_key'

lock = Lock()
INDEX = {}
SITEMAP_STATUS = {}

app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://objskwxzxzuvdd:a7c3fa0a58658cb7b21dc6dae288945d6553533a378259af35abd16d707beb09@ec2-34-226-11-94.compute-1.amazonaws.com:5432/dbsdd3r2uu4alq'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

class SearchQuery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    query = db.Column(db.String(500))

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
    except Exception as e:
        logging.error(f"Failed to index URL: {url}", exc_info=True)


@app.route("/submit", methods=["GET", "POST"])
def submit():
    if request.method == "POST":
        sitemap_url = request.form["sitemap_url"]
        new_sitemap = SubmittedSitemap(url=sitemap_url, indexing_status='Added to queue', status='Not started', total_urls=0, indexed_urls=0)
        db.session.add(new_sitemap)
        db.session.commit()
        process_sitemap_queue()
        flash("Sitemap submitted successfully.")
        return redirect(url_for('submit'))  # Redirect back to the same page
    else:
        return render_template("submit.html")  # Render the submit page if the request method is GET

@app.route("/dashboard", methods=["GET"])
def dashboard():
    return render_template("dashboard.html", sitemaps=SubmittedSitemap.query.all())

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
    sitemap_url = urllib.parse.unquote(sitemap_url)
    if sitemap_url in SITEMAP_STATUS:
        del SITEMAP_STATUS[sitemap_url]
        flash('Sitemap has been deleted successfully')
    else:
        flash('Sitemap URL not found')
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    app.run(debug=True, threaded=True)
