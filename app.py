import os
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

app = Flask(__name__)
app.secret_key = 'your_secret_key'

lock = Lock()
INDEX = {}
SITEMAP_STATUS = {}

app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://objskwxzxzuvdd:a7c3fa0a58658cb7b21dc6dae288945d6553533a378259af35abd16d707beb09@ec2-34-226-11-94.compute-1.amazonaws.com:5432/dbsdd3r2uu4alq'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

SITEMAP_QUEUE = queue.Queue()
MAX_SIMULTANEOUS_INDEXING = 5
CURRENTLY_INDEXING = 0

@app.route('/', methods=['GET', 'POST'])
def search():
    if request.method == 'POST':
        query = request.form.get('query').lower()

        results = {
            url: {**data, 'title': escape(data['title']), 'description': escape(data['description'])}
            for url, data in INDEX.items()
            if query in data['title'].lower() or query in url.lower() or query in data['description'].lower()
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

def index_sitemap(sitemap_url):
    global CURRENTLY_INDEXING, INDEX

    SITEMAP_STATUS[sitemap_url] = {'status': 'Indexing started...'}
    urls = get_urls_from_sitemap(sitemap_url)
    SITEMAP_STATUS[sitemap_url]['total_urls'] = len(urls)

    for url in urls:
        if sitemap_url not in SITEMAP_STATUS:
            print(f"Stopped indexing for deleted sitemap: {sitemap_url}")
            return
        else:
            try:
                index_url(url)
            except Exception as e:
                print(f"Error occurred while indexing URL {url}: {e}")

    CURRENTLY_INDEXING -= 1
    SITEMAP_STATUS[sitemap_url]['status'] = 'Indexing finished'

    process_sitemap_queue()

def get_urls_from_sitemap(sitemap_url):
    response = requests.get(sitemap_url)
    soup = BeautifulSoup(response.text, "html.parser")
    urls = [loc.text for loc in soup.find_all("loc")]
    return urls

def index_url(url):
    res = requests.get(url)
    page_soup = BeautifulSoup(res.text, "html.parser")
    title = page_soup.find("title").text if page_soup.find("title") else url
    description = page_soup.find("meta", attrs={"name": "description"})
    description = description["content"] if description else "No description available"
    INDEX[url] = {"title": title, "description": description}

@app.route("/submit", methods=["GET", "POST"])
def submit():
    if request.method == "POST":
        sitemap_url = request.form["sitemap_url"]
        SITEMAP_QUEUE.put(sitemap_url)
        SITEMAP_STATUS[sitemap_url] = 'Added to queue'
        process_sitemap_queue()
        flash("Sitemap submitted successfully.")
        return redirect(url_for('submit'))  # Redirect back to the same page
    else:
        return render_template("submit.html")  # Render the submit page if the request method is GET

@app.route("/dashboard", methods=["GET"])
def dashboard():
    return render_template("dashboard.html", sitemap_status=SITEMAP_STATUS)

@app.route("/urls", methods=["GET"])
def urls():
    return render_template("urls.html", index=INDEX)

@app.route('/all_search_queries', methods=['GET'])
def all_search_queries():
    # Retrieving all search queries from the database
    # queries = ... (your code to get the data)
    
    # For the sake of example, let's create a mock list of queries
    queries = ['query 1', 'query 2', 'query 3']

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
    app.run(debug=True)