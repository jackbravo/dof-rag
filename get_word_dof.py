#!/usr/bin/env python3
# /// script
# requires-python = ">=3.7"
# dependencies = [
#     "requests",
#     "typer",
#     "beautifulsoup4",
#     "urllib3",
# ]
# ///
"""
Script to download WORD files from the Official Gazette of the Federation (DOF)
Simplified version - Only downloads WORD files

"""

import logging
import re
import ssl
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin

import requests
import typer
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Constants
MIN_FILE_SIZE = 1024  # Minimum file size in bytes for validation
ERROR_COUNT = 0
HTML_PREFIXES = (b"<!doctype html", b"<html")


class TLSAdapter(HTTPAdapter):
    """Custom adapter to force TLS 1.2 or 1.3"""

    def __init__(self, ssl_context=None, **kwargs):
        self.ssl_context = ssl_context or ssl.create_default_context()
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        return super().init_poolmanager(*args, **kwargs)


def setup_session() -> requests.Session:
    """
    Configures a requests session with custom TLS adapter
    
    This function disables SSL certificate verification for compatibility
    with the DOF website.
    """
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    ssl_context.set_ciphers("DEFAULT@SECLEVEL=1")
    
    session = requests.Session()
    session.mount('https://', TLSAdapter(ssl_context=ssl_context))
    session.verify = False
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    return session


def extract_word_links(html_content: str, base_url: str = 'https://www.dof.gob.mx') -> List[tuple[str, str]]:
    """
    Extracts WORD file links from HTML content
    
    Args:
        html_content: HTML content of the page
        base_url: Base URL to build absolute links
        
    Returns:
        List of tuples (url_word, codnota)
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    word_links = []
    
    word_anchors = soup.find_all('a', href=re.compile(r'/nota_to_doc\.php\?codnota=\d+'))
    
    for anchor in word_anchors:
        href = anchor.get('href')
        if href:
            match = re.search(r'codnota=(\d+)', href)
            if match:
                codnota = match.group(1)
                full_url = urljoin(base_url, href)
                word_links.append((full_url, codnota))
    
    return word_links


def extract_notice_links(html_content: str) -> List[tuple[str, str]]:
    """
    Extracts notice links from SIDOF HTML content for all AVISOS subsections
    Detects edition (MAT/VES) based on tab-pane container
    
    Args:
        html_content: HTML content of the SIDOF page
        
    Returns:
        List of tuples (note_id, edition) where edition is 'MAT' or 'VES'
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    notice_links = []
    
    # Find all spans with class "txt-traduct" that contain " AVISOS "
    avisos_spans = soup.find_all('span', class_='txt-traduct', string=re.compile(r'^\s*AVISOS\s*$'))
    
    for avisos_span in avisos_spans:
        # Detect edition by finding the tab-pane container
        tab_pane = avisos_span.find_parent('div', class_='tab-pane')
        if not tab_pane:
            continue
            
        tab_id = tab_pane.get('id')
        edition = None
        if tab_id == 'resp-tab2':  # Vespertina
            edition = 'VES'
        elif tab_id == 'resp-tab3':  # Matutina
            edition = 'MAT'
        
        if not edition:
            continue
        
        panel_heading = avisos_span.find_parent('div', class_='panel-heading')
        if not panel_heading:
            continue
        
        parent_panel = panel_heading.find_parent('div', class_='panel-default')
        if not parent_panel:
            continue
        
        note_links = parent_panel.find_all('a', href=re.compile(r'/notas/\d+'))
        
        for link in note_links:
            href = link.get('href')
            if href:
                match_id = re.search(r'/notas/(\d+)', href)
                if match_id:
                    note_id = match_id.group(1)
                    notice_links.append((note_id, edition))
    
    return notice_links


def is_valid_dof_listing(
    html_content: str, date_str: str, edition: str
) -> bool:
    """Recognize a populated listing or an explicit validated empty date."""
    soup = BeautifulSoup(html_content, "html.parser")
    title = soup.title.get_text(" ", strip=True).casefold() if soup.title else ""
    if "diario oficial de la federación" not in title:
        return False
    if soup.find(id="cuerpo_principal") is None:
        return False

    text = " ".join(soup.stripped_strings).casefold()
    edition_name = {"MAT": "matutina", "VES": "vespertina"}.get(edition)
    if not edition_name:
        return False
    expected_header = f"fecha: {date_str} - edición {edition_name}".casefold()
    return (
        expected_header in text
        or "no hay datos para la fecha seleccionada" in text
    )


def is_valid_sidof_listing(
    html_content: str, day: str, month: str, year: str
) -> bool:
    """Require the dated SIDOF publication shell before accepting no notices."""
    soup = BeautifulSoup(html_content, "html.parser")
    title = soup.title.get_text(" ", strip=True).casefold() if soup.title else ""
    if "diario oficial de la federación" not in title:
        return False
    if f"{day}-{month}-{year}" not in html_content:
        return False
    return soup.find(id="resp-tab2") is not None or soup.find(id="resp-tab3") is not None


def _download_file(session: requests.Session, url: str, output_path: Path, file_type: str = "file") -> bool:
    """
    Internal function to download a file from a URL
    
    Args:
        session: Configured requests session
        url: URL of the file to download
        output_path: Path where to save the file
        file_type: Type of file for logging purposes (default: "file")
        
    Returns:
        True if download was successful, False otherwise
    """
    global ERROR_COUNT
    try:
        logging.info(f"Downloading {file_type}: {url}")
        
        response = session.get(url, timeout=30)
        response.raise_for_status()
        
        if not is_valid_word_payload(response.content):
            logging.error(f"Invalid WORD payload returned by {url}")
            if output_path.exists() and not is_valid_word_file(output_path):
                output_path.unlink()
            ERROR_COUNT += 1
            return False

        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_suffix(output_path.suffix + ".part")
        with open(temporary, 'wb') as f:
            f.write(response.content)
        temporary.replace(output_path)

        if is_valid_word_file(output_path):
            logging.info(f"Downloaded successfully: {output_path}")
            return True
        else:
            logging.warning(f"Invalid file (missing or too small), deleting: {output_path}")
            if output_path.exists():
                output_path.unlink()
            ERROR_COUNT += 1
            return False
            
    except Exception as e:
        logging.error(f"Error downloading {url}: {e}")
        if output_path.exists():
            output_path.unlink()
        ERROR_COUNT += 1
        return False


def download_word_file(session: requests.Session, url: str, output_path: Path) -> bool:
    """
    Downloads a WORD file from the specified URL
    """
    return _download_file(session, url, output_path, file_type="WORD file")


def download_notice_file(session: requests.Session, note_id: str, output_path: Path) -> bool:
    """
    Downloads a notice file from SIDOF
    """
    url = f"https://sidof.segob.gob.mx/notas/getDoc/{note_id}"
    return _download_file(session, url, output_path, file_type="notice")


def is_valid_word_payload(content: bytes) -> bool:
    """Reject DOF/SIDOF HTML error pages masquerading as Word downloads."""
    if len(content) < MIN_FILE_SIZE:
        return False
    prefix = content[:512].lstrip().lower()
    return not prefix.startswith(HTML_PREFIXES)


def is_valid_word_file(path: Path) -> bool:
    try:
        if path.stat().st_size < MIN_FILE_SIZE:
            return False
        with path.open("rb") as stream:
            prefix = stream.read(512).lstrip().lower()
        return not prefix.startswith(HTML_PREFIXES)
    except OSError:
        return False


def has_valid_download(date_dir: Path, note_id: str) -> bool:
    """Return whether this note exists under any page-order sequence number."""
    return any(is_valid_word_file(path) for path in date_dir.glob(f"*_{note_id}.doc"))


def _create_edition_dir(output_dir: Path, day: str, month: str, year: str, edition: str) -> Path:
    """Creates and returns edition directory path"""
    date_dir = output_dir / year / month / f"{day}{month}{year}" / edition
    date_dir.mkdir(parents=True, exist_ok=True)
    return date_dir


def process_sidof_notices(session: requests.Session, day: str, month: str, year: str, edition: str, output_dir: Path, sleep_delay: float = 1.0, start_index: int = 0) -> int:
    """
    Processes SIDOF page to download AVISOS notices for specific edition
    
    Args:
        session: Configured requests session
        day: Day component (DD)
        month: Month component (MM)
        year: Year component (YYYY)
        edition: Edition ('MAT' or 'VES') to filter notices
        output_dir: Base directory to save files
        sleep_delay: Time to wait between downloads in seconds (default: 1.0)
        
    Returns:
        Number of notice files downloaded successfully
    """
    global ERROR_COUNT
    sidof_url = f"https://sidof.segob.gob.mx/welcome/{day}-{month}-{year}"
    
    try:
        logging.info(f"Processing SIDOF page: {sidof_url}")
        response = session.get(sidof_url, timeout=30)
        response.raise_for_status()

        if not is_valid_sidof_listing(response.text, day, month, year):
            ERROR_COUNT += 1
            logging.error(
                f"SIDOF returned an unrecognized listing for "
                f"{day}/{month}/{year}; refusing to validate an empty date"
            )
            return 0
        
        notice_links = extract_notice_links(response.text)
        
        if not notice_links:
            logging.info(f"No notices found for {day}/{month}/{year} in SIDOF")
            return 0
        
        filtered_notices = [(nid, ed) for nid, ed in notice_links if ed == edition]
        
        logging.info(f"Found {len(notice_links)} total notices in SIDOF, {len(filtered_notices)} for {edition} edition")
        
        if not filtered_notices:
            logging.info(f"No notices found for {edition} edition")
            return 0
        
        date_dir = _create_edition_dir(output_dir, day, month, year, edition)
        
        downloaded_count = 0
        
        for index, (note_id, _) in enumerate(filtered_notices, start=start_index):
            filename = f"{str(index+1).zfill(3)}_AVISO_{year}{month}{day}_{edition}_{note_id}.doc"
            output_path = date_dir / filename
            
            if has_valid_download(date_dir, note_id):
                logging.info(f"Notice already downloaded: {note_id}")
                continue
            
            if download_notice_file(session, note_id, output_path):
                downloaded_count += 1
            
            time.sleep(sleep_delay)
        
        return downloaded_count
        
    except Exception as e:
        ERROR_COUNT += 1
        logging.error(f"Error processing SIDOF page {sidof_url}: {e}")
        return 0


def process_dof_page(session: requests.Session, date_str: str, edition: str, output_dir: Path, sleep_delay: float = 1.0) -> int:
    """
    Processes a DOF page to download WORD files only
    
    Args:
        session: Configured requests session
        date_str: Date in DD/MM/YYYY format
        edition: Edition ('MAT' or 'VES')
        output_dir: Base directory to save files
        sleep_delay: Time to wait between downloads in seconds (default: 1.0)
        
    Returns:
        Number of files downloaded successfully
    """
    global ERROR_COUNT
    day, month, year = date_str.split('/')
    
    dof_url = f"https://www.dof.gob.mx/index.php?year={year}&month={month}&day={day}&edicion={edition}"
    
    try:
        logging.info(f"Processing page: {dof_url}")
        response = session.get(dof_url, timeout=30)
        response.raise_for_status()
        
        if is_valid_dof_listing(response.text, date_str, edition):
            word_links = extract_word_links(response.text)
        else:
            ERROR_COUNT += 1
            logging.error(
                f"DOF returned an unrecognized listing for {date_str} - {edition}; "
                "refusing to validate an empty date"
            )
            word_links = []
        downloaded_count = 0

        if not word_links:
            # No Word documents on the main page does not imply the edition
            # is empty: SIDOF notices may still exist for this date.
            logging.info(
                f"No WORD files found for {date_str} - {edition}; "
                "still checking SIDOF notices")
        else:
            logging.info(f"Found {len(word_links)} WORD files")

            date_dir = _create_edition_dir(output_dir, day, month, year, edition)

            for index, (word_url, codnota) in enumerate(word_links):
                filename = f"{str(index+1).zfill(3)}_DOF_{year}{month}{day}_{edition}_{codnota}.doc"
                output_path = date_dir / filename

                if has_valid_download(date_dir, codnota):
                    logging.info(f"WORD note already downloaded: {codnota}")
                    continue

                if download_word_file(session, word_url, output_path):
                    downloaded_count += 1

                time.sleep(sleep_delay)

        logging.info(f"Now processing SIDOF notices for {date_str} - {edition}")
        notices_downloaded = process_sidof_notices(session, day, month, year, edition, output_dir, sleep_delay, start_index=len(word_links))
        downloaded_count += notices_downloaded
        
        return downloaded_count
        
    except Exception as e:
        ERROR_COUNT += 1
        logging.error(f"Error processing page {dof_url}: {e}")
        return 0


def main(
    date: str = typer.Argument(..., help="Fecha (DD/MM/YYYY) o fecha de inicio para rango"),
    end_date: Optional[str] = typer.Argument(None, help="Fecha de fin (DD/MM/YYYY) - opcional para rango de fechas"),
    output_dir: str = typer.Option("./dof_word", help="Directorio de salida"),
    editions: str = typer.Option("both", help="Ediciones a descargar: 'mat', 'ves', o 'both'"),
    log_level: str = typer.Option("INFO", help="Nivel de logging: DEBUG, INFO, WARNING, ERROR"),
    sleep_delay: float = typer.Option(1.0, help="Tiempo de espera en segundos entre descargas")
):
    """
    Downloads WORD files from the Official Gazette of the Federation
    Includes regular WORD files from dof.gob.mx and AVISOS notices from sidof.segob.gob.mx
    
    Usage examples:
    # For a specific date (uses ./dof_word by default):
    python get_word_dof.py 02/01/2023 --editions both
    
    # For a date range:
    python get_word_dof.py 01/01/2023 31/01/2023 --editions both
    
    # Specifying custom directory:
    python get_word_dof.py 02/01/2023 --output-dir ./my_folder --editions both
    
    # Controlling download speed with sleep_delay:
    python get_word_dof.py 02/01/2023 --sleep-delay 0.5   # Fast downloads (0.5s between files)
    python get_word_dof.py 02/01/2023 --sleep-delay 2.0   # Slow downloads (2s between files)
    python get_word_dof.py 02/01/2023 --sleep-delay 0.1   # Very fast (0.1s - use carefully)
    
    # Complete example:
    python get_word_dof.py 01/01/2023 31/01/2023 --output-dir ./dof --editions both --sleep-delay 1.5
    """
    
    global ERROR_COUNT
    ERROR_COUNT = 0

    log_levels = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR
    }
    
    logging.basicConfig(
        level=log_levels.get(log_level.upper(), logging.INFO),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('word_download.log'),
            logging.StreamHandler()
        ]
    )
    
    try:
        start_dt = datetime.strptime(date, "%d/%m/%Y")
        
        if end_date is None:
            end_dt = start_dt
        else:
            end_dt = datetime.strptime(end_date, "%d/%m/%Y")
            
    except ValueError:
        logging.error("Dates must be in DD/MM/YYYY format")
        sys.exit(1)
    
    if start_dt > end_dt:
        logging.error("Start date must be before end date")
        sys.exit(1)
    
    editions = editions.lower()
    if editions not in ['mat', 'ves', 'both']:
        logging.error("Editions must be 'mat', 'ves', or 'both'")
        sys.exit(1)
    
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    session = setup_session()
    
    logging.info("Starting DOF WORD files download")
    
    if end_date is None:
        logging.info(f"Date: {date}")
    else:
        logging.info(f"Period: {date} - {end_date}")
    
    logging.info(f"Editions: {editions}")
    logging.info(f"Output directory: {output_path.absolute()}")
    logging.info("-" * 60)
    
    total_downloaded = 0
    current_date = start_dt
    
    while current_date <= end_dt:
        date_str = current_date.strftime("%d/%m/%Y")
        
        editions_to_process = []
        if editions in ['mat', 'both']:
            editions_to_process.append('MAT')
        if editions in ['ves', 'both']:
            editions_to_process.append('VES')
        
        for edition in editions_to_process:
            downloaded = process_dof_page(session, date_str, edition, output_path, sleep_delay)
            total_downloaded += downloaded
        
        current_date += timedelta(days=1)
    
    logging.info("-" * 60)
    logging.info(f"Download completed. Total files downloaded: {total_downloaded}")
    if ERROR_COUNT:
        logging.error(f"Download completed with {ERROR_COUNT} error(s)")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    typer.run(main)
