import os
import pickle
from flask import Flask, render_template, request, redirect, url_for, flash
from bs4 import BeautifulSoup
import requests
import threading
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

SITEMAP_FILE = 'sitemaps.pkl'
TOTAL_INDEXED_PAGES = 0  # variable to keep track of the total number of pages indexed
TOTAL_SEARCHES = 0  # variable to keep track of the total number of searches
SEARCH_QUERIES = {}  # dictionary to keep track of each search query along with its frequency

app.config['SQLALCHEMY_DATABASE_URI'] = 'postgres://objskwxzxzuvdd:a7c3fa0a58658cb7b21dc6dae288945d6553533a378259af35abd16d707beb09@ec2-34-226-11-94.compute-1.amazonaws.com:5432/dbsdd3r2uu4alq'  # Update with your Heroku Postgres connection details
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

SITEMAP_QUEUE = queue.Queue()
MAX_SIMULTANEOUS_INDEXING = 5
CURRENTLY_INDEXING = 0

def calculate_relevance_score(query, data):
    # Implement your own function to calculate the relevance score based on the search query and data for a URL
    # You can consider factors like the presence and frequency of query terms in the title, description, and content of the page
    # Assign a numerical score representing the relevance of the page to the search query
    # Higher scores indicate higher relevance

    title = data['title'].lower()
    description = data['description'].lower()

    # Example: Relevance scoring based on the presence of query terms in the title and description
    score = 0
    title_matches = title.count(query)
    description_matches = description.count(query)

    # Increase the score for multiple instances of the search query in the title or description
    score += title_matches
    score += description_matches

    # Add additional points for an exact match of the search query
    if query in title:
        score += 5
    if query in description:
        score += 3

    return score

@app.route('/', methods=['GET', 'POST'])
def search():
    global TOTAL_SEARCHES, SEARCH_QUERIES, INDEX

    query = None  # Initialize query variable here
    results = {}
    ad = {}  # Initialize ad variable here

    if request.method == 'POST':
        query = request.form.get('query').lower()
        TOTAL_SEARCHES += 1  # increment total number of searches
        SEARCH_QUERIES[query] = SEARCH_QUERIES.get(query, 0) + 1  # increment search query frequency

        results = {
            url: {**data, 'title': escape(data['title']), 'description': escape(data['description'])}
            for url, data in INDEX.items()
            if query in data['title'].lower() or query in url.lower() or query in data['description'].lower()
        }

        # Calculate relevance score for each result based on the search query
        for url, data in results.items():
            relevance_score = calculate_relevance_score(query, data)  # Implement your own function to calculate the relevance score
            results[url]['relevance_score'] = relevance_score

        # Sort the results based on relevance score
        results = dict(sorted(results.items(), key=lambda x: x[1]['relevance_score'], reverse=True))

        # Pick a random URL for the ad
        ad_url = None
        while ad_url is None or results[ad_url]['type'] == 'image':
            ad_url = random.choice(list(results.keys()))

        ad = {
            'title': results[ad_url]['title'],
            'url': ad_url,
            'description': results[ad_url]['description']
        }

    if request.method == 'POST':
        return render_template('results.html', query=query, results=results, ad=ad)
    return render_template('search.html')


def process_sitemap_queue():
    global CURRENTLY_INDEXING
    while not SITEMAP_QUEUE.empty() and CURRENTLY_INDEXING < MAX_SIMULTANEOUS_INDEXING:
        sitemap_url = SITEMAP_QUEUE.get()
        SITEMAP_STATUS[sitemap_url] = 'Waiting to start indexing...'
        indexing_thread = threading.Thread(target=index_sitemap, args=(sitemap_url,))
        indexing_thread.start()
        CURRENTLY_INDEXING += 1


def index_sitemap(sitemap_url):
    global CURRENTLY_INDEXING, TOTAL_INDEXED_PAGES, INDEX
    SITEMAP_STATUS[sitemap_url] = {'status': 'Indexing started...'}

    try:
        response = requests.get(sitemap_url)
        soup = BeautifulSoup(response.text, "xml")
        urls = [loc.text for loc in soup.find_all("loc")]

        # Add the total number of URLs to SITEMAP_STATUS
        SITEMAP_STATUS[sitemap_url]['total_urls'] = len(urls)
        SITEMAP_STATUS[sitemap_url]['indexed_urls'] = 0

        image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.svg', '.bmp', '.webp']

        for url in urls:
            if sitemap_url not in SITEMAP_STATUS:
                print(f"Stopped indexing for deleted sitemap: {sitemap_url}")
                return
            
            if url.endswith('.xml'):
                SITEMAP_QUEUE.put(url)
                SITEMAP_STATUS[url] = 'Added to queue'
                process_sitemap_queue()
            else:
                try:
                    res = requests.get(url)
                    page_soup = BeautifulSoup(res.text, "html.parser")
                    title = page_soup.find("title").text if page_soup.find("title") else url
                    description = page_soup.find("meta", attrs={"name": "description"})
                    description = description["content"] if description else "No description available"

                    url_type = "webpage"

                    if any(re.search(ext, url) for ext in image_extensions):
                        url_type = "image"

                    new_data = {
                        "title": title,
                        "description": description,
                        "type": url_type
                    }

                    if url in INDEX:
                         if INDEX[url] != new_data:
                            INDEX[url] = new_data
                            TOTAL_INDEXED_PAGES += 1  # increment total number of pages indexed
                            SITEMAP_STATUS[sitemap_url]['indexed_urls'] += 1
                            print(f"Updated index for URL {url}")

                            indexed_url = IndexedURL.query.filter_by(url=url).first()
                            if indexed_url:
                                indexed_url.title = title
                                indexed_url.description = description
                                indexed_url.type = url_type
                            else:
                                indexed_url = IndexedURL(url=url, title=title, description=description, type=url_type)
                                db.session.add(indexed_url)
                    else:
                        INDEX[url] = new_data
                        TOTAL_INDEXED_PAGES += 1  # increment total number of pages indexed
                        SITEMAP_STATUS[sitemap_url]['indexed_urls'] += 1
                        print(f"Added URL {url} to index")

                        indexed_url = IndexedURL(url=url, title=title, description=description, type=url_type)
                        db.session.add(indexed_url)
                    
                    db.session.commit()

                except Exception as e:
                    print(f"Error occurred while indexing URL {url}: {e}")

    finally:
        CURRENTLY_INDEXING -= 1
        SITEMAP_STATUS[sitemap_url]['status'] = 'Indexing finished'

        
        process_sitemap_queue()


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
    top_search_queries = dict(sorted(SEARCH_QUERIES.items(), key=lambda item: item[1], reverse=True)[:10])
    top_search_queries_first_half = list(top_search_queries.items())[:5]
    top_search_queries_second_half = list(top_search_queries.items())[5:]
    return render_template("dashboard.html", 
                           sitemap_status=SITEMAP_STATUS, 
                           total_pages=TOTAL_INDEXED_PAGES, 
                           total_searches=TOTAL_SEARCHES, 
                           top_search_queries_first_half=top_search_queries_first_half, 
                           top_search_queries_second_half=top_search_queries_second_half)


@app.route("/urls", methods=["GET"])
def urls():
    index = IndexedURL.query.all()
    return render_template("urls.html", index=index)


@app.route('/delete_sitemap')
def delete_sitemap():
    sitemap_url = request.args.get('sitemap_url')
    sitemap_url = urllib.parse.unquote(sitemap_url)
    sitemap_entry = SITEMAP_STATUS.get(sitemap_url)
    if sitemap_entry:
        db.session.query(IndexedURL).filter(IndexedURL.url.startswith(sitemap_url)).delete(synchronize_session=False)
        db.session.commit()
        del SITEMAP_STATUS[sitemap_url]
        flash('Sitemap and corresponding URLs have been deleted successfully')
    else:
        flash('Sitemap URL not found')
    return redirect(url_for('dashboard'))


@app.route('/all_search_queries')
def all_search_queries():
    # Get all search queries, not just top 10
    all_search_queries = dict(sorted(SEARCH_QUERIES.items(), key=lambda item: item[1], reverse=True))
    return render_template('all_search_queries.html', all_search_queries=all_search_queries)


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
