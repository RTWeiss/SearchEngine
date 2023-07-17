import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

indexed_urls = {}

import threading

# Add a lock for the indexing process
index_lock = threading.Lock()
indexed_data = {}

def index_sitemap(sitemap_url):
    response = requests.get(sitemap_url)
    soup = BeautifulSoup(response.text, 'xml')
    urls = soup.find_all('loc')
    for url in urls:
        url = url.text
        index_lock.acquire()
        if url not in indexed_data:
            index_lock.release()
            try:
                response = requests.get(url)
                page_soup = BeautifulSoup(response.text, 'html.parser')
                title = page_soup.title.string if page_soup.title else url
                description_tag = page_soup.find('meta', attrs={'name': 'description'})
                description = description_tag['content'] if description_tag else 'No description available'
                index_lock.acquire()
                indexed_data[url] = {'title': title, 'description': description}
                index_lock.release()
            except:
                pass
        else:
            index_lock.release()
            

