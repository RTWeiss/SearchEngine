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
from sqlalchemy import func
from concurrent.futures import ThreadPoolExecutor, as_completed
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
db = SQLAlchemy(app)
executor = ThreadPoolExecutor(max_workers=MAX_SIMULTANEOUS_INDEXING)
semaphore = Semaphore(MAX_SIMULTANEOUS_INDEXING)
SITEMAP_QUEUE = queue.Queue()

logging.basicConfig(filename='app.log', filemode='w', format='%(name)s - %(levelname)s - %(message)s', level=logging.DEBUG)

class SearchQuery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    search_term = db.Column(db.String(500))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    frequency = db.Column(db.Integer, default=1)

class SubmittedSitemap(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.Text, nullable=False)
    indexing_status = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(100), nullable=False)  
    total_urls = db.Column(db.Integer, nullable=False)
    indexed_urls = db.relationship('IndexedURL', backref='sitemap', lazy=True)  # Keep this line

class IndexedURL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.Text, nullable=False)
    title = db.Column(db.String(500), nullable=True)
    description = db.Column(db.Text, nullable=True)
    type = db.Column(db.String(50), nullable=True)
    sitemap_id = db.Column(db.Integer, db.ForeignKey('submitted_sitemap.id'), nullable=False)
    def __init__(self, url, title, description, type, sitemap_id):
        self.url = url
        self.title = title
        self.description = description
        self.type = type
        self.sitemap_id = sitemap_id

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

def decrement_currently_indexing():
    global CURRENTLY_INDEXING
    with lock:
        CURRENTLY_INDEXING -= 1

def increment_currently_indexing():
    global CURRENTLY_INDEXING
    with lock:
        CURRENTLY_INDEXING += 1

@app.route("/submit", methods=["GET", "POST"])
def submit():
    if request.method == "POST":
        sitemap_url = request.form["sitemap_url"]
        new_sitemap = SubmittedSitemap(url=sitemap_url, indexing_status='In queue', status='Not started', total_urls=0)
        try:
            db.session.add(new_sitemap)
            db.session.commit()
            semaphore.acquire()  # Control the number of simultaneous indexings
            future = executor.submit(index_sitemap, sitemap_url, new_sitemap.id)  # call index_sitemap function here
            future.add_done_callback(lambda x: semaphore.release())  # Release the semaphore when done
            return redirect(url_for('submit'))
        except Exception as e:
            logging.error(f"An error occurred while saving the sitemap: {e}", exc_info=True)
            flash("An error occurred while saving the sitemap. Please try again.")
            return render_template("submit.html"), 500
    else:
        return render_template("submit.html")
    
def start_background_thread():
    while True:
        try:
            process_sitemap_queue()
        except Exception as e:
            logging.error(f"Error occurred while processing sitemap queue: {e}", exc_info=True)
        time.sleep(5)

def process_sitemap_queue():
    while not SITEMAP_QUEUE.empty():
        semaphore.acquire()  # Control the number of simultaneous indexings
        sitemap_url = SITEMAP_QUEUE.get()
        sitemap = SubmittedSitemap.query.filter_by(url=sitemap_url).first()
        if sitemap:
            sitemap.indexing_status = 'Indexing'
            db.session.commit()
        try:
            index_sitemap(sitemap_url, sitemap.id)  # Pass sitemap.id as well
            if sitemap:
                sitemap.indexing_status = 'Completed'
                db.session.commit()
        except Exception as e:
            logging.error(f"Error occurred while indexing sitemap: {e}", exc_info=True)
            if sitemap:
                sitemap.indexing_status = 'Failed'
                db.session.commit()
        finally:
            semaphore.release()  # Release the semaphore after indexing is completed

def index_sitemap(sitemap_url, sitemap_id):
    try:
        urls = get_urls_from_sitemap(sitemap_url)
        sitemap = SubmittedSitemap.query.get(sitemap_id)
        if sitemap:
            sitemap.indexing_status = 'Indexing'
            try:
                sitemap.total_urls = len(urls)  # Update total_urls
                db.session.commit()
            except Exception as e:
                logging.error(f"An error occurred while saving total_urls: {str(e)}", exc_info=True)
        for url in urls:
            index_url(url, sitemap.id)  # Pass sitemap_id here
        if sitemap:
            sitemap.indexing_status = 'Completed'  # Update status after indexing is done
            db.session.commit()
    except Exception as e:
        logging.error(f"An error occurred while indexing sitemap: {sitemap_url}. Error: {str(e)}", exc_info=True)
        sitemap = SubmittedSitemap.query.get(sitemap_id)
        if sitemap:
            sitemap.indexing_status = 'Failed'
            db.session.commit()


def index_url(url, sitemap_id):  # sitemap argument is sitemap_id here
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()  # Will raise an HTTPError if the response was unsuccessful
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to fetch {url}: {e}", exc_info=True)
        return

    soup = BeautifulSoup(response.text, "html.parser")
    title = soup.find("title")
    title = title.text if title else "N/A"

    description_tag = soup.find("meta", attrs={"name": "description"})
    description = description_tag.get("content") if description_tag else "N/A"

    indexed_url = IndexedURL(url=url, title=title, description=description, type=None, sitemap_id=sitemap_id)  # Added type=None here

    db.session.add(indexed_url)
    db.session.commit()  # Commit the changes here
    print(f"Indexed {url}")

def get_urls_from_sitemap(sitemap_url):
    try:
        response = requests.get(sitemap_url)
        soup = BeautifulSoup(response.text, "xml")
    except Exception as e:
        logging.error(f"Error parsing XML from {sitemap_url}: {e}", exc_info=True)
        return []

    urls = []

    # Handle normal sitemap with <url> tags
    try:
        url_tags = soup.find_all("url")
        urls.extend([url.loc.string for url in url_tags])
    except Exception as e:
        logging.error(f"Error getting URLs from XML {sitemap_url}: {e}", exc_info=True)

    # Handle sitemap index files with <sitemap> tags
    try:
        sitemap_tags = soup.find_all("sitemap")
        for sitemap in sitemap_tags:
            sitemap_response = requests.get(sitemap.loc.string)
            sitemap_soup = BeautifulSoup(sitemap_response.text, "xml")
            url_tags = sitemap_soup.find_all("url")
            urls.extend([url.loc.string for url in url_tags])
    except Exception as e:
        logging.error(f"Error getting sitemap from XML {sitemap_url}: {e}", exc_info=True)

    # Handle case where sitemap contains links to other .xml files
    try:
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
    except Exception as e:
        logging.error(f"Error getting XML loc tags from {sitemap_url}: {e}", exc_info=True)

    return urls


def update_sitemap(sitemap, status, total_urls=None, indexed_urls=None):
    with lock:
        sitemap.indexing_status = status
        sitemap.status = status  # Update status field as well
        if total_urls is not None:
            sitemap.total_urls = total_urls
        db.session.commit()
        if indexed_urls is not None:
            for url in indexed_urls:
                new_url = IndexedURL(url=url, sitemap_id=sitemap.id)  # Adjust this line according to your IndexedURL model
                db.session.add(new_url)
            db.session.commit()

@app.route("/dashboard", methods=["GET"])
def dashboard():
    try:
        submitted_sitemaps = SubmittedSitemap.query.all()
        sitemap_status = {
            sitemap.url: {
                'indexing_status': sitemap.indexing_status,
                'total_urls': sitemap.total_urls,
                'indexed_urls': IndexedURL.query.filter_by(sitemap_id=sitemap.id).count(),  # Use SQLAlchemy query to count indexed urls
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
    time.sleep(1)
    if not thread.is_alive():
        logging.error("Background thread failed to start.")
    app.run(debug=True, threaded=True)

if __name__ == "__main__":
    run_app()
