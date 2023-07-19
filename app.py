import os
import logging
import threading
import time
from flask import Flask, render_template, request, redirect, url_for, flash
from bs4 import BeautifulSoup
import requests
import urllib.parse
from threading import Lock, Semaphore, Thread
import queue
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import escape
from datetime import datetime
from sqlalchemy import func
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import copy_current_request_context
from sqlalchemy.orm import sessionmaker


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

CURRENTLY_INDEXING = 0

logging.basicConfig(filename='app.log', filemode='w', format='%(name)s - %(levelname)s - %(message)s', level=logging.DEBUG)

def get_urls_from_sitemap(sitemap_url):
    urls = []

    try:
        response = requests.get(sitemap_url)
        if response.status_code == 200:  # Add check for response status
            soup = BeautifulSoup(response.text, "xml")
            
            # Handle normal sitemap with <url> tags
            url_tags = soup.find_all("url")
            urls.extend([url.loc.string for url in url_tags])

            # Handle sitemap index files with <sitemap> tags
            sitemap_tags = soup.find_all("sitemap")
            for sitemap in sitemap_tags:
                sitemap_response = requests.get(sitemap.loc.string)
                if sitemap_response.status_code == 200:  # Add check for response status
                    sitemap_soup = BeautifulSoup(sitemap_response.text, "xml")
                    url_tags = sitemap_soup.find_all("url")
                    urls.extend([url.loc.string for url in url_tags])

            # Handle case where sitemap contains links to other .xml files
            xml_loc_tags = soup.find_all("loc")
            for xml_url in xml_loc_tags:
                # Checking if the url is of an xml file
                if xml_url.text.endswith('.xml'):
                    # Make a request to the xml file
                    xml_response = requests.get(xml_url.text)
                    if xml_response.status_code == 200:  # Add check for response status
                        xml_soup = BeautifulSoup(xml_response.text, "xml")
                        # Extract all urls from the xml file
                        xml_url_tags = xml_soup.find_all("loc")
                        urls.extend([url.string for url in xml_url_tags])

    except requests.exceptions.RequestException as e:
        logging.error(f"Error getting XML from {sitemap_url}: {e}", exc_info=True)

    except Exception as e:
        logging.error(f"Unexpected error getting URLs from XML {sitemap_url}: {e}", exc_info=True)

    logging.info(f"Found {len(urls)} URLs in sitemap: {sitemap_url}")
    return urls

def index_url(url, sitemap_id):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to fetch {url}: {e}", exc_info=True)
        return

    soup = BeautifulSoup(response.text, "html.parser")
    title = soup.find("title")
    title = title.text if title else "N/A"

    description_tag = soup.find("meta", attrs={"name": "description"})
    description = description_tag.get("content") if description_tag else "N/A"

    indexed_url = IndexedURL(url=url, title=title, description=description, type=None, sitemap_id=sitemap_id)

    with app.app_context():
        db.session.add(indexed_url)
        db.session.commit()

    logging.info(f"Indexed URL: {url}")
    print(f"Indexed {url}")


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


@app.route("/submit", methods=["GET", "POST"])
def submit():
    if request.method == "POST":
        sitemap_url = request.form["sitemap_url"]
        urls = get_urls_from_sitemap(sitemap_url)
        new_sitemap = SubmittedSitemap(url=sitemap_url, indexing_status='In queue', status='In queue', total_urls=len(urls))
        try:
            db.session.add(new_sitemap)
            db.session.commit()
            SITEMAP_QUEUE.put(sitemap_url)
            return redirect(url_for('submit'))
        except Exception as e:
            logging.error(f"An error occurred while saving the sitemap: {e}", exc_info=True)
            flash("An error occurred while saving the sitemap. Please try again.")
            return render_template("submit.html"), 500
    else:
        return render_template("submit.html")

def index_sitemap(sitemap_url, sitemap_id):
    try:
        urls = get_urls_from_sitemap(sitemap_url)
        for url in urls:
            index_url(url, sitemap_id)
            
        sitemap = SubmittedSitemap.query.get(sitemap_id)
        if sitemap:
            update_sitemap(sitemap, 'Completed')
    except Exception as e:
        logging.error(f"An error occurred while indexing sitemap: {sitemap_url}. Error: {str(e)}", exc_info=True)
        sitemap = SubmittedSitemap.query.get(sitemap_id)
        if sitemap:
            update_sitemap(sitemap, 'Failed')

@app.route("/dashboard", methods=["GET"])
def dashboard():
    try:
        submitted_sitemaps = SubmittedSitemap.query.all()
        sitemap_status = {
            sitemap.url: {
                'indexing_status': sitemap.indexing_status,
                'total_urls': sitemap.total_urls,
                'indexed_urls': len(sitemap.indexed_urls),  # Count indexed urls related to each sitemap
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

def process_sitemap_queue():
    with app.app_context():
        while True:
            semaphore.acquire()  # add semaphore here to limit the number of concurrent threads
            sitemap_url = SITEMAP_QUEUE.get()  # blocking get
            sitemap = SubmittedSitemap.query.filter_by(url=sitemap_url).first()
            if sitemap:
                sitemap.indexing_status = 'Indexing'
                db.session.commit()
                try:
                    future = executor.submit(index_sitemap, sitemap_url, sitemap.id)
                    future.add_done_callback(lambda x: update_sitemap_status(x, sitemap))
                    SITEMAP_QUEUE.task_done()  # mark task as done
                except Exception as e:
                    logging.error(f"Error occurred while indexing sitemap: {e}", exc_info=True)
                    sitemap.indexing_status = 'Failed'
                    db.session.commit()
            semaphore.release()  # release semaphore when finished with task

def update_sitemap_status(future, sitemap):
    exception = future.exception()
    if exception:
        sitemap.indexing_status = 'Failed'
        logging.error(f"Error occurred while indexing sitemap: {exception}", exc_info=True)
    else:
        sitemap.indexing_status = 'Completed'
    db.session.commit()

def start_background_thread():
    thread = threading.Thread(target=process_sitemap_queue, daemon=True)
    thread.start()

def run_app():
    thread = threading.Thread(target=start_background_thread, daemon=True)  # target=start_background_thread
    thread.start()
    time.sleep(1)
    if not thread.is_alive():
        logging.error("Background thread failed to start.")
    app.run(debug=True, threaded=True)

if __name__ == "__main__":
    run_app()

