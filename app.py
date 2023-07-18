import os
import re
import threading
import random
import requests
import queue
from urllib.parse import urljoin, urlparse
from flask import Flask, render_template, request, redirect, url_for, flash
from bs4 import BeautifulSoup
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects import postgresql
from dotenv import load_dotenv
from html import escape

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')

db = SQLAlchemy(app, session_options={"autoflush": False}, engine_options={"connect_args": {"check_same_thread": False}}, engine_cls=postgresql.dialect())

DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL

class SearchQuery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    query = db.Column(db.String(256), unique=False, nullable=False)
    count = db.Column(db.Integer, unique=False, nullable=False)


class SiteMap(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sitemap_url = db.Column(db.String(2048), unique=True, nullable=False)
    status = db.Column(db.String(256), unique=False, nullable=False)
    total_urls = db.Column(db.Integer, unique=False, nullable=True)
    indexed_urls = db.Column(db.Integer, unique=False, nullable=True)


class IndexedURL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(2048), unique=True, nullable=False)
    title = db.Column(db.String(256), unique=False, nullable=False)
    description = db.Column(db.String(2048), unique=False, nullable=False)
    type = db.Column(db.String(64), unique=False, nullable=False)


try:
    db.create_all()
    db.session.commit()
except Exception as e:
    print(f"Error occurred while creating tables: {e}")

SITEMAP_QUEUE = queue.Queue()
SITEMAP_STATUS = {}
INDEX = {}
MAX_SIMULTANEOUS_INDEXING = 5
CURRENTLY_INDEXING = 0


@app.route('/', methods=['GET', 'POST'])
def search():
    results = {}
    ad = {}

    if request.method == 'POST':
        query = request.form.get('query').lower()

        search_query = SearchQuery.query.filter_by(query=query).first()
        if search_query is None:
            search_query = SearchQuery(query=query, count=1)
        else:
            search_query.count += 1
        db.session.add(search_query)
        db.session.commit()

        results = {
            url: {**data, 'title': escape(data['title']), 'description': escape(data['description'])}
            for url, data in INDEX.items()
            if query in data['title'].lower() or query in url.lower() or query in data['description'].lower()
        }

        for url, data in results.items():
            relevance_score = calculate_relevance_score(query, data)
            results[url]['relevance_score'] = relevance_score

        results = dict(sorted(results.items(), key=lambda x: x[1]['relevance_score'], reverse=True))
        ad = get_ad(results)

    return render_template('results.html', query=query, results=results, ad=ad) if request.method == 'POST' else render_template('search.html')


def calculate_relevance_score(query, data):
    title = data['title'].lower()
    description = data['description'].lower()

    score = 0
    title_matches = title.count(query)
    description_matches = description.count(query)

    score += title_matches
    score += description_matches

    if query in title:
        score += 5
    if query in description:
        score += 3

    return score


def get_ad(results):
    ad_url = None
    while ad_url is None or results[ad_url]['type'] == 'image':
        ad_url = random.choice(list(results.keys()))

    ad = {
        'title': results[ad_url]['title'],
        'url': ad_url,
        'description': results[ad_url]['description']
    }
    return ad


@app.route("/submit", methods=["GET", "POST"])
def submit():
    if request.method == "POST":
        sitemap_url = request.form["sitemap_url"]
        SITEMAP_QUEUE.put(sitemap_url)
        SITEMAP_STATUS[sitemap_url] = 'Added to queue'
        process_sitemap_queue()
        flash("Sitemap submitted successfully.")
        return redirect(url_for('submit'))

    return render_template("submit.html")


def process_sitemap_queue():
    global CURRENTLY_INDEXING
    while not SITEMAP_QUEUE.empty() and CURRENTLY_INDEXING < MAX_SIMULTANEOUS_INDEXING:
        sitemap_url = SITEMAP_QUEUE.get()
        SITEMAP_STATUS[sitemap_url] = 'Waiting to start indexing...'
        indexing_thread = threading.Thread(target=index_sitemap, args=(sitemap_url,))
        indexing_thread.start()
        CURRENTLY_INDEXING += 1


def index_sitemap(sitemap_url):
    global CURRENTLY_INDEXING, TOTAL_INDEXED_PAGES
    SITEMAP_STATUS[sitemap_url] = {'status': 'Indexing started...'}

    try:
        response = requests.get(sitemap_url)
        soup = BeautifulSoup(response.text, "xml")
        urls = [loc.text for loc in soup.find_all("loc")]

        SITEMAP_STATUS[sitemap_url]['total_urls'] = len(urls)
        SITEMAP_STATUS[sitemap_url]['indexed_urls'] = 0

        image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.svg', '.bmp', '.webp']

        for url in urls:
            if url not in INDEX:
                try:
                    page_response = requests.get(url)
                    page_soup = BeautifulSoup(page_response.text, "html.parser")
                    title = page_soup.title.string if page_soup.title else 'No title'
                    description_tag = page_soup.find('meta', attrs={'name': 'description'})

                    if description_tag:
                        description = description_tag['content']
                    else:
                        description = 'No description'

                    type = 'image' if any(ext in url for ext in image_extensions) else 'webpage'

                    INDEX[url] = {'title': title, 'description': description, 'type': type}
                    SITEMAP_STATUS[sitemap_url]['status'] = 'Indexing in progress...'
                    SITEMAP_STATUS[sitemap_url]['indexed_urls'] += 1
                    TOTAL_INDEXED_PAGES += 1

                except Exception as e:
                    print(f"Exception occurred while indexing URL {url}: {e}")

        SITEMAP_STATUS[sitemap_url]['status'] = 'Indexing completed.'
        CURRENTLY_INDEXING -= 1
        process_sitemap_queue()

    except Exception as e:
        SITEMAP_STATUS[sitemap_url] = {'status': f'Exception occurred: {e}'}
        CURRENTLY_INDEXING -= 1
        process_sitemap_queue()


if __name__ == "__main__":
    app.run(debug=True)
