"""
IIBX RAG Data Pipeline
======================
Steps:
  1. Scrapes all HTML content pages on iibx.co.in and writes to iibx_scrape.json
  2. Scrapes all PDF links and downloads PDFs into ./backend/data/pdfs/
  3. Runs ingest.py to chunk, embed, and store everything in ChromaDB

Requirements:
  pip install requests beautifulsoup4 pymupdf chromadb sentence-transformers

Run:
  python iibx_pipeline.py
"""

import os
import re
import time
import json
import subprocess
import sys
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

# ── Configuration ────────────────────────────────────────────────────────────

# Pages to scrape for HTML text content → goes into iibx_scrape.json
CONTENT_URLS = [
    "https://www.iibx.co.in/static/about.aspx",
    "https://www.iibx.co.in/static/objective_roles.aspx",
    "https://www.iibx.co.in/static/boardofdirectors.aspx",
    "https://www.iibx.co.in/static/management.aspx",
    "https://www.iibx.co.in/static/group_companies_new.aspx",
    "https://www.iibx.co.in/static/Committees_under_Regulation.aspx",
    "https://www.iibx.co.in/static/recognition.aspx",
    "https://www.iibx.co.in/static/regulations.aspx",
    "https://www.iibx.co.in/static/IFSC_Authority.aspx",
    "https://www.iibx.co.in/static/iibx_regulations.aspx",
    "https://www.iibx.co.in/static/faq.aspx",
    # Spot market products
    "https://www.iibx.co.in/static/spot.aspx",
    "https://www.iibx.co.in/static/gold995_specifications.aspx",
    "https://www.iibx.co.in/static/goldmini999_specifications.aspx",
    "https://www.iibx.co.in/static/Gold9999_specifications.aspx",
    "https://www.iibx.co.in/static/Gold12_9999_specifications.aspx",
    "https://www.iibx.co.in/static/UAE_GD_GOLD_995.aspx",
    "https://www.iibx.co.in/static/UAEGD_GOLD_999.aspx",
    "https://www.iibx.co.in/static/UAEGD_GOLD9999_specifications.aspx",
    "https://www.iibx.co.in/static/UAEGD_GOLD12_9999_specifications.aspx",
    "https://www.iibx.co.in/static/UAEGDTRQ_GOLD_995.aspx",
    "https://www.iibx.co.in/static/UAEGDTRQ_GOLD_999.aspx",
    "https://www.iibx.co.in/static/Silver_Grains.aspx",
    "https://www.iibx.co.in/static/UAEGD_Silver_Grains.aspx",
    "https://www.iibx.co.in/static/UAEGD_CEPA_Silver_Grains.aspx",
    "https://www.iibx.co.in/static/Silver_Bar.aspx",
    "https://www.iibx.co.in/static/UAEGD_Bar.aspx",
    # Membership & operations
    "https://www.iibx.co.in/static/trading_system.aspx",
    "https://www.iibx.co.in/static/settlement_schedule.aspx",
    "https://www.iibx.co.in/static/circulars_notifications.aspx",
    # Market data & news
    "https://www.iibx.co.in/markets/knowledgecenter.aspx",
    "https://www.iibx.co.in/markets/PressRelease.aspx",
    "https://www.iibx.co.in/markets/Circular.aspx",
    "https://www.iibx.co.in/markets/dailymarketdata.aspx",
    "https://www.iibx.co.in/static/event.aspx",
    "https://www.iibx.co.in/index.aspx",
    # Futures market
    "https://derivative.iibx.co.in/",
    "https://derivative.iibx.co.in/GoldFuture",
]

# Pages to scrape for PDF links only
PDF_SCRAPE_URLS = [
    "https://www.iibx.co.in/",
    "https://www.iibx.co.in/static/spot.aspx",
    "https://www.iibx.co.in/static/trading_system.aspx",
    "https://www.iibx.co.in/static/settlement_schedule.aspx",
    "https://www.iibx.co.in/static/IFSC_Authority.aspx",
    "https://derivative.iibx.co.in/",
    "https://derivative.iibx.co.in/GoldFuture",
]

# Known PDFs discovered via Google (seed list)
SEED_PDFS = [
    "https://www.iibx.co.in/download/BDR_creation_extinguishment_process_flow.pdf",
    "https://www.iibx.co.in/download/User_manual_for_Online_Trade_file_Activation_and_Installation_IIBX.pdf",
    "https://www.iibx.co.in/download/IIBX_MEMBERS_DIRECTORY.pdf",
    "https://www.iibx.co.in/download/IIBX_Exchange_Rules.pdf",
    "https://www.iibx.co.in/download/TheIndiaIBXIFSCLimitedEcosystem.pdf",
    "https://derivative.iibx.co.in/Download/Annexure1.pdf",
    "https://derivative.iibx.co.in/Download/Contract_Specifications_Gold_Futures.pdf",
    "https://derivative.iibx.co.in/Download/File_Format_Trading_v1.0.pdf",
    "https://www.iibx.co.in/download/circulars/IIBX%20Gold%20Futures%20Presentation%2029112024$845f7d0c-c43b-49c8-ab05-b28597344ba3.pdf",
]

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
PDF_DIR      = os.path.join(BASE_DIR, "backend", "data", "pdfs")
SCRAPE_JSON  = os.path.join(BASE_DIR, "backend", "data", "iibx_scrape.json")
HEADERS      = {"User-Agent": "Mozilla/5.0 (compatible; IIBXBot/1.0)"}

# Nav/footer boilerplate to strip from scraped text
NAV_NOISE = re.compile(
    r"(About Us|Regulation|Spot Market|Futures Market|Technology|Membership|"
    r"Market Operations|Market Participants|Refiners|Depository|Grievances|"
    r"Feedback|Contact Us|Copyright ©|Disclaimer|Terms of Use|"
    r"Circulars|Media|Knowledge Center|Event|Career)\s*",
    re.IGNORECASE,
)


# ── Step 1: Scrape HTML content pages ────────────────────────────────────────

def scrape_page_text(url: str) -> str:
    """Fetch a page and return its visible text, nav boilerplate stripped."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove script, style, nav, footer elements
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        text = soup.get_text(separator=" ")
        text = re.sub(r"\s+", " ", text).strip()
        # Strip repeated nav links that appear on every page
        text = NAV_NOISE.sub(" ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text
    except Exception as e:
        print(f"  [warn] could not scrape {url}: {e}")
        return ""


def build_scrape_json(content_urls: list, out_path: str):
    """Scrape all content pages and write to iibx_scrape.json."""
    entries = []
    for url in content_urls:
        print(f"  scraping {url}")
        text = scrape_page_text(url)
        if text:
            entries.append({"url": url, "text": text})
        time.sleep(0.4)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    print(f"  [✓] wrote {len(entries)} pages to {out_path}")
    return entries


# ── Step 2: Scrape pages for PDF links ───────────────────────────────────────

def scrape_pdf_links(base_urls: list) -> list:
    found = set(SEED_PDFS)
    for url in base_urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup.find_all("a", href=True):
                href = tag["href"]
                full = urljoin(url, href)
                if full.lower().endswith(".pdf") or "/download/" in full.lower():
                    found.add(full)
            print(f"  scraped {url} → {len(found)} PDFs so far")
        except Exception as e:
            print(f"  [warn] could not scrape {url}: {e}")
        time.sleep(0.5)
    return list(found)


# ── Step 3: Download PDFs ─────────────────────────────────────────────────────

def safe_filename(url: str) -> str:
    name = os.path.basename(urlparse(url).path)
    name = re.sub(r"[^\w\-.]", "_", name)
    return name or "unnamed.pdf"


def download_pdfs(pdf_urls: list, out_dir: str) -> list:
    os.makedirs(out_dir, exist_ok=True)
    downloaded = []
    for url in pdf_urls:
        fname = safe_filename(url)
        dest = os.path.join(out_dir, fname)
        if os.path.exists(dest):
            print(f"  [skip] {fname} already exists")
            downloaded.append(dest)
            continue
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30, stream=True)
            if resp.status_code == 200 and "pdf" in resp.headers.get("content-type", "").lower():
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(8192):
                        f.write(chunk)
                print(f"  [ok]   {fname}")
                downloaded.append(dest)
            else:
                print(f"  [skip] {url} → HTTP {resp.status_code}")
        except Exception as e:
            print(f"  [err]  {url}: {e}")
        time.sleep(0.3)
    return downloaded


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  IIBX RAG Data Pipeline")
    print("=" * 60)

    # 1. Scrape HTML content → iibx_scrape.json
    print(f"\n[1/3] Scraping {len(CONTENT_URLS)} content pages → {SCRAPE_JSON}")
    build_scrape_json(CONTENT_URLS, SCRAPE_JSON)

    # 2. Scrape PDF links + download
    print(f"\n[2/3] Scraping PDF links and downloading to {PDF_DIR}/")
    pdf_urls = scrape_pdf_links(PDF_SCRAPE_URLS)
    print(f"      Found {len(pdf_urls)} PDF URLs")
    pdf_paths = download_pdfs(pdf_urls, PDF_DIR)
    print(f"      Downloaded/verified {len(pdf_paths)} PDFs")

    # 3. Run ingest.py
    print("\n[3/3] Running ingest.py to embed and store in ChromaDB...")
    result = subprocess.run(
        [sys.executable, "ingest.py"],
        cwd=os.path.join(BASE_DIR, "backend"),
    )
    if result.returncode == 0:
        print("\n[✓] Pipeline complete!")
    else:
        print("\n[!] ingest.py exited with errors — check output above.")


if __name__ == "__main__":
    main()
