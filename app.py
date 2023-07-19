import os
import logging
import time
from flask import Flask, render_template, request, redirect, url_for, flash
from bs4 import BeautifulSoup
import requests
import threading
import urllib.parse
from threading import Lock
import queue
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import escape
from datetime import datetime
from models import db, IndexedURL
from sqlalchemy import func
from concurrent.futures import ThreadPoolExecutor
from threading import Semaphore

MAX_SIMULTANEOUS_INDEXING = 5

DATABASE_URL = os.getenv('DATABASE_URL')

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app = Flask(__name__)
app.secret_key = 'your_secret_key'

lock = Lock()

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

executor = ThreadPoolExecutor(max_workers=MAX_SIMULTANEOUS_INDEXING)
semaphore = Semaphore(MAX_SIMULTANEOUS_INDEXING)

class SearchQuery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    search_term = db.Column(db.String(500))  # Change this line
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    frequency = db.Column(db.Integer, default=1)


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

logging.basicConfig(filename='app.log', filemode='w', format='%(name)s - %(levelname)s - %(message)s', level=logging.DEBUG)

@app.route('/', methods=['GET', 'POST'])
def search():
    if request.method == 'POST':
        search_term = request.form.get('query').lower()  
        new_query = SearchQuery(search_term=search_term)  
        db.session.add(new_query)
        db.session.commit()

        results = IndexedURL.query.filter(
            IndexedURL.url.contains(search_term) |  # Change query to search_term
            IndexedURL.title.contains(search_term) |  # Change query to search_term
            IndexedURL.description.contains(search_term)  # Change query to search_term
        ).all()
        return render_template('results.html', query=search_term, results=results)  # Change query to search_term
    return render_template('search.html')

def start_background_thread():
    while True:
        try:
            if not SITEMAP_QUEUE.empty():
                process_sitemap_queue()
        except Exception as e:
            logging.error(f"Error occurred while processing sitemap queue: {e}", exc_info=True)
        time.sleep(5)

def increment_currently_indexing():
    global CURRENTLY_INDEXING
    with lock:
        CURRENTLY_INDEXING += 1

def decrement_currently_indexing():
    global CURRENTLY_INDEXING
    with lock:
        CURRENTLY_INDEXING -= 1

def update_sitemap(sitemap, status, total_urls=None, indexed_urls=None):
    with lock:
        sitemap.indexing_status = status
        if total_urls is not None:
            sitemap.total_urls = total_urls
        if indexed_urls is not None:
            sitemap.indexed_urls = indexed_urls
    db.session.commit()

def process_sitemap_queue():
    while not SITEMAP_QUEUE.empty():
        semaphore.acquire()
        sitemap_url = SITEMAP_QUEUE.get()
        logging.info(f'Starting a new indexing thread for: {sitemap_url}')
        executor.submit(index_sitemap, sitemap_url)

def index_sitemap(sitemap_url):
    global CURRENTLY_INDEXING
    logging.info(f'Called index_sitemap for: {sitemap_url}')

    urls = get_urls_from_sitemap(sitemap_url)
    logging.info(f'Found {len(urls)} URLs in sitemap: {sitemap_url}')

    sitemap = SubmittedSitemap.query.filter_by(url=sitemap_url).first()
    if sitemap:
        with lock:
            sitemap.total_urls = len(urls)
            sitemap.indexing_status = 'Indexing'  
            db.session.commit()

    for url in urls:
        try:
            index_url(url, 'sitemap', sitemap)  # Here I added 'sitemap' as the type
        except Exception as e:
            logging.error(f"Error occurred while indexing URL {url}: {e}", exc_info=True)

    with lock:
        CURRENTLY_INDEXING -= 1

        if sitemap:
            sitemap.indexing_status = 'Completed'
            db.session.commit()

def get_urls_from_sitemap(sitemap_url):
    response = requests.get(sitemap_url)
    soup = BeautifulSoup(response.text, "xml")
    urls = []

    # Handle normal sitemap with <url> tags
    url_tags = soup.find_all("url")
    urls.extend([url.loc.string for url in url_tags])

    # Handle sitemap index files with <sitemap> tags
    sitemap_tags = soup.find_all("sitemap")
    for sitemap in sitemap_tags:
        sitemap_response = requests.get(sitemap.loc.string)
        sitemap_soup = BeautifulSoup(sitemap_response.text, "xml")
        url_tags = sitemap_soup.find_all("url")
        urls.extend([url.loc.string for url in url_tags])

    # Handle case where sitemap contains links to other .xml files
    # Assuming these links are in <loc> tags
    xml_loc_tags = soup.find_all("loc")
    for xml_url in xml_loc_tags:
        # Checking if the url is of an xml file
        if xml_url.text.endswith('.xml'):
            # Make a request to the xml file
            xml_response = requests.get(xml_url.text)
            xml_soup = BeautifulSoup(xml_response.text, "xml")
            # Extract all urls from the xml file
            xml_url_tags = xml_soup.find_all("loc")
            urls.extend([url.string for url in xml_url_tags])

    return urls

def index_url(url, url_type, sitemap):  # New parameter 'url_type' added here
    try:
        res = requests.get(url)
        page_soup = BeautifulSoup(res.text, "html.parser")
        title = page_soup.find("title").text if page_soup.find("title") else url
        description = page_soup.find("meta", attrs={"name": "description"})
        description = description["content"] if description else "No description available"

        new_indexed_url = IndexedURL(url=url, title=title, description=description, type=url_type)
        db.session.add(new_indexed_url)
        if sitemap:
            with lock:
                sitemap.indexed_urls = sitemap.indexed_urls + 1 if sitemap.indexed_urls else 1
        db.session.commit()
    except Exception as e:
        logging.error(f"Failed to index URL: {url}", exc_info=True)

@app.route("/submit", methods=["GET", "POST"])
def submit():
    if request.method == "POST":
        sitemap_url = request.form["sitemap_url"]
        total_urls = len(get_urls_from_sitemap(sitemap_url))  # get total urls at this point
        new_sitemap = SubmittedSitemap(url=sitemap_url, indexing_status='Added to queue', status='Not started',
                                       total_urls=total_urls, indexed_urls=0)
        db.session.add(new_sitemap)
        db.session.commit()
        SITEMAP_QUEUE.put(sitemap_url)

        # Update the status of the SubmittedSitemap
        with lock:
            new_sitemap.status = 'Started'
            db.session.commit()

        executor.submit(process_sitemap_queue)  # submit to executor here

        flash("Sitemap submitted successfully.")
        return redirect(url_for('submit'))
    else:
        return render_template("submit.html")


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

        # Limit the results to top 10 and order by frequency
        search_queries = (db.session.query(SearchQuery.search_term, func.sum(SearchQuery.frequency))
                      .group_by(SearchQuery.search_term)
                      .order_by(func.sum(SearchQuery.frequency).desc())
                      .limit(10)
                      .all())
        
        # Calculate total_pages and total_searches
        total_pages = sum([sitemap.total_urls for sitemap in submitted_sitemaps])
        total_searches = SearchQuery.query.count()

        return render_template('dashboard.html', total_pages=total_pages, search_queries=search_queries, sitemap_status=sitemap_status)
    except Exception as e:
        logging.error(f"An error occurred while loading the dashboard: {e}", exc_info=True)
        return str(e), 500


@app.route("/urls", methods=["GET"])
def urls():
    return render_template("urls.html")

@app.route('/all_search_queries', methods=['GET'])
def all_search_queries():
    search_queries = db.session.query(SearchQuery.search_term, func.sum(SearchQuery.frequency)).group_by(SearchQuery.search_term).order_by(SearchQuery.search_term).all()
    return render_template('all_search_queries.html', search_queries=search_queries)

@app.route('/delete_sitemap')
def delete_sitemap():
    sitemap_url = request.args.get('sitemap_url')
    if sitemap_url is None:
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

def run_app():
    thread = threading.Thread(target=start_background_thread, daemon=True)
    thread.start()
    if not thread.is_alive():
        logging.error("Background thread failed to start.")
    app.run(debug=True, threaded=True)

if __name__ == "__main__":
    run_app()
