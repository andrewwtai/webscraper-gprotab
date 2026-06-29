# file: gprotab_scraper.py
import os
import re
import sys
import uuid
import time
import json
import shutil
import subprocess
import argparse
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
import requests
from bs4 import BeautifulSoup

# --- Constants & Config ---
BASE_URL = "https://gprotab.net/"
FILES_PER_FOLDER = 500
LOCAL_ROOT = "gprotab"
UUID_TRUNC_LEN = 8

# Style keywords used to route artists into the 'composer' field instead of 'artist_band_name'
COMPOSER_STYLE_KEYWORDS = frozenset({
    "classical", "baroque", "romantic", "renaissance", "medieval", 
    "modern classical", "contemporary classical", "traditional", "folk", 
    "sacred", "religious", "hymn", "chorale", "carol", "christmas", 
    "wedding", "patriotic", "national anthem", "march", "ragtime", 
    "spiritual", "gospel", "ceremonial", "early music", "classic"
})

# Default MuseScore Executable paths by OS
MUSESCORE_PATHS = [
    # Windows common locations
    r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
    r"C:\Program Files\MuseScore 3\bin\MuseScore3.exe",
    # macOS standard App bundle
    "/Applications/MuseScore 4.app/Contents/MacOS/mscore",
    "/Applications/MuseScore 3.app/Contents/MacOS/mscore",
    # Linux system path fallbacks
    "musescore4",
    "musescore",
    "mscore"
]

# --- Helper Functions ---
def find_musescore_executable():
    """Attempt to locate MuseScore executable on your machine."""
    for path in MUSESCORE_PATHS:
        if Path(path).is_file(): 
            return path
        # If it's a raw system binary name, check if it exists in system PATH
        if "/" not in path and "\\" not in path:
            which = shutil.which(path)
            if which: return which
    return None

def sanitize_filename(filename):
    """Removes invalid characters and handles empty strings."""
    stem = re.sub(r'[\\/*?:"<>|]', "", filename).strip()
    if not stem:
        return "untitled_tab"
    return stem[:50]  # Limit to 50 characters to prevent Windows 260-char path crashes [41]

def setup_dirs():
    root = Path(LOCAL_ROOT)
    for d in ["metadata", "music_data", "error_pages"]:
        (root / d).mkdir(parents=True, exist_ok=True)

# --- Checkpoint & Logging System ---
def log_checkpoint(url, status, error=""):
    """Logs the success or failure of a URL to allow safe resuming."""
    checkpoint_file = Path(LOCAL_ROOT) / "checkpoint.jsonl"
    record = {
        "url": url,
        "status": status,
        "error": error,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")
    }
    with open(checkpoint_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def load_processed_urls():
    """Reads checkpoints and returns a set of URLs we have already completed."""
    checkpoint_file = Path(LOCAL_ROOT) / "checkpoint.jsonl"
    processed = set()
    if checkpoint_file.exists():
        with open(checkpoint_file, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    # Skip it if it succeeded, or if it was a 404 not found
                    if data.get("status") == "success" or data.get("error") == "404":
                        processed.add(data["url"])
                except:
                    pass
    return processed

def log_failed_url(url, stage="discovery"):
    """Logs any URL that permanently failed after maximum network retries."""
    failed_file = Path(LOCAL_ROOT) / "failed_urls.jsonl"
    record = {
        "url": url, 
        "stage": stage, 
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")
    }
    with open(failed_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"[-] Permanent network failure on {url}. Recorded to failed_urls.jsonl.")

# --- Network Tolerant Fetchers ---
def fetch_html_with_retry(url, max_retries=10, retry_delay=180):
    """Fetches HTML content, waiting up to 30 minutes for connection drops to resolve."""
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            print(f"\n[-] Network drop at {url}. Attempt {attempt}/{max_retries}. Waiting {retry_delay}s...")
            time.sleep(retry_delay)
        except requests.exceptions.HTTPError as e:
            # If it is a standard 404 (not found), skip it instantly without waiting
            if e.response.status_code == 404:
                print(f"\n[-] Page not found (404) at {url}. Skipping.")
                return "404"
            print(f"\n[-] HTTP Error {e.response.status_code} at {url}. Retrying in 10s...")
            time.sleep(10)
    
    # Exceeded all retries
    log_failed_url(url, stage="html_fetch")
    return None

def download_file_with_retry(url, dest, max_retries=10, retry_delay=180):
    """Downloads files safely, checking for zero-byte corruptions."""
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, stream=True, timeout=30)
            r.raise_for_status()
            with open(dest, "wb") as f:
                shutil.copyfileobj(r.raw, f)
            # Failsafe: Check if file is empty
            if os.path.getsize(dest) == 0:
                raise Exception("Downloaded file is 0 bytes.")
            return True
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            print(f"\n[-] Connection dropped during download of {url}. Attempt {attempt}/{max_retries}. Waiting {retry_delay}s...")
            time.sleep(retry_delay)
        except Exception as e:
            print(f"\n[-] Download error: {e}. Retrying in 10s...")
            time.sleep(10)
            
    # Exceeded all retries
    log_failed_url(url, stage="file_download")
    return False

# --- Crawler & Discovery Loops ---
def get_artists_by_letter(letter):
    """Scrapes band page links starting with a given letter."""
    url = urljoin(BASE_URL, f"en/artists/{letter.lower()}")
    print(f"[*] Fetching artist listings for letter '{letter}'...")
    html = fetch_html_with_retry(url)
    if not html or html == "404":
        return []
        
    soup = BeautifulSoup(html, "html.parser")
    artists = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/en/tabs/" in href and len(href.split("/")) == 4:
            artists.append(urljoin(BASE_URL, href))
    return sorted(list(set(artists)))

def get_songs_by_artist(artist_url):
    """Scrapes individual tab page links for a given artist."""
    html = fetch_html_with_retry(artist_url)
    if not html or html == "404":
        return []
        
    soup = BeautifulSoup(html, "html.parser")
    songs = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        parts = href.split("/")
        if "/en/tabs/" in href and len(parts) == 5:
            songs.append(urljoin(BASE_URL, href))
    return sorted(list(set(songs)))

def run_discovery():
    """Builds cache for all tabs listed alphabetically, saving incrementally to handle crashes."""
    cache_file = Path(LOCAL_ROOT) / "discovery_cache.json"
    scanned_file = Path(LOCAL_ROOT) / "scanned_artists.json"
    
    all_song_urls = []
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                all_song_urls = json.load(f)
        except Exception:
            pass
            
    scanned_artists = set()
    if scanned_file.exists():
        try:
            with open(scanned_file, "r", encoding="utf-8") as f:
                scanned_artists = set(json.load(f))
        except Exception:
            pass
    
    letters = "abcdefghijklmnopqrstuvwxyz"
    print(f"[*] Starting discovery. Already scanned {len(scanned_artists)} artists. Loaded {len(all_song_urls)} cached URLs.")
    
    try:
        for l in letters:
            artists = get_artists_by_letter(l)
            for artist in artists:
                if artist in scanned_artists:
                    continue 
                    
                songs = get_songs_by_artist(artist)
                all_song_urls.extend(songs)
                all_song_urls = sorted(list(set(all_song_urls)))
                scanned_artists.add(artist)
                
                # Save progress to disk immediately
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(all_song_urls, f, indent=2)
                with open(scanned_file, "w", encoding="utf-8") as f:
                    json.dump(list(scanned_artists), f, indent=2)
                    
                print(f"[+] Discovered {len(songs)} song links for {artist} (Total Cached: {len(all_song_urls)})")
                time.sleep(1.5)
                
    except KeyboardInterrupt:
        print("\n[-] Discovery paused by user. Progress saved safely.")
    except Exception as e:
        print(f"\n[-] Discovery encountered an error: {e}. Progress saved safely up to last artist.")
        
    return all_song_urls

def load_cached_discovery():
    cache_file = Path(LOCAL_ROOT) / "discovery_cache.json"
    if cache_file.exists():
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

# --- Heuristics & Conversion ---
def pop_vs_classical_split(artist_name, styles):
    """Routes Classical styles to composer, Modern styles to artist."""
    if not artist_name: return None, None
    style_lows = [s.lower() for s in styles]
    artist_low = artist_name.lower()
    is_composer = ("trad" in artist_low or "traditional" in artist_low or 
                   any(k in s for k in COMPOSER_STYLE_KEYWORDS for s in style_lows))
    if is_composer: return None, artist_name
    return artist_name, None

def get_batch_folder_path(piece_index):
    batch_num = piece_index // FILES_PER_FOLDER
    return Path(LOCAL_ROOT) / "music_data" / f"batch_{batch_num:04d}"

def convert_gp_to_midi(musescore_bin, gp_file_path, midi_output_path):
    """Calls MuseScore headlessly to output standard MIDI formats."""
    if not musescore_bin: return False
    try:
        # Added "-f" to force MuseScore to ignore minor score corruptions in conversion mode [1.1.9, 1.2.1]
        cmd = [musescore_bin, "-f", "-o", str(midi_output_path), str(gp_file_path)]
        env = os.environ.copy()
        if sys.platform != "win32": env["QT_QPA_PLATFORM"] = "offscreen"
        
        # 60 second timeout: stops MuseScore from hanging forever on corrupted files
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        if result.returncode == 0 and midi_output_path.exists() and os.path.getsize(midi_output_path) > 0:
            return True
    except subprocess.TimeoutExpired:
        print("[-] MuseScore conversion timed out (corrupted file).")
    except Exception as e:
        print(f"[-] MuseScore error: {e}")
    return False

# --- Parsing ---
def parse_tab_page(song_url):
    """Parses tab info from GProTab page table details."""
    html = fetch_html_with_retry(song_url)
    if not html: return None
    if html == "404": return "404"
        
    soup = BeautifulSoup(html, "html.parser")
    metadata = {
        "song_title": None, "artist_name": None, "tab_type": "gp5",
        "file_size": None, "downloads": None, "rating": None, "styles": []
    }
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            label = tds[0].get_text(strip=True).lower().rstrip(":")
            val = tds[1].get_text(strip=True)
            if "band" in label: metadata["artist_name"] = val
            elif "song" in label: metadata["song_title"] = val
            elif "tab file type" in label: metadata["tab_type"] = val
            elif "file size" in label: metadata["file_size"] = val
            elif "times downloaded" in label: metadata["downloads"] = val
            elif "rating" in label: metadata["rating"] = val
            
    for a in soup.find_all("a", href=True):
        if "/style/" in a["href"].lower() or "style" in a.get("class", []):
            metadata["styles"].append(a.get_text(strip=True))
    return metadata

# --- Core Scrape Logic ---
def scrape_song(song_url, piece_index, musescore_bin):
    meta = parse_tab_page(song_url)
    if meta == "404":
        log_checkpoint(song_url, "failed", error="404")
        return False
    if not meta:
        log_checkpoint(song_url, "failed", error="HTML fetch failed")
        return False
    
    song_id = str(uuid.uuid4())
    artist_name = meta["artist_name"] or "Unknown Artist"
    song_title = meta["song_title"] or "Untitled Track"
    tab_type = meta["tab_type"] or "gp5"
    artist_band, composer = pop_vs_classical_split(artist_name, meta["styles"])
    
    orig_filename = f"{sanitize_filename(song_title)}.{tab_type}"
    generated_stem = f"{sanitize_filename(song_title)}_{song_id[:UUID_TRUNC_LEN]}"
    
    batch_dir = get_batch_folder_path(piece_index)
    song_dir = batch_dir / song_id
    source_file_dir = song_dir / "source_file"
    midi_file_dir = song_dir / "midi_files" / "raw"
    proj_file_dir = song_dir / "multimodal_files" / "project" / "tablature_project"
    
    for d in [source_file_dir, midi_file_dir, proj_file_dir]:
        d.mkdir(parents=True, exist_ok=True)
        
    download_url = f"{song_url}?download=1"
    gp_path_src = source_file_dir / f"{generated_stem}.{tab_type}"
    gp_path_proj = proj_file_dir / f"{generated_stem}.{tab_type}"
    
    print(f"[*] Downloading: {orig_filename} ...", end=" ")
    if not download_file_with_retry(download_url, gp_path_src):
        shutil.rmtree(song_dir, ignore_errors=True) # Cleanup on fail
        log_checkpoint(song_url, "failed", error="Download failed")
        return False
        
    shutil.copy(gp_path_src, gp_path_proj)
    
    midi_output_path = midi_file_dir / f"{generated_stem}.mid"
    if not convert_gp_to_midi(musescore_bin, gp_path_src, midi_output_path):
        print("Conversion failed.")
        shutil.rmtree(song_dir, ignore_errors=True) # Cleanup on fail
        log_checkpoint(song_url, "failed", error="MIDI conversion failed")
        return False
    
    print("Success.")
        
    # --- Generate Metadata Schema ---
    retrieval_metadata = {
        "source_info": [{
            "song_id": song_id,
            "midi_file_versions": {
                "raw": midi_output_path.relative_to(Path(LOCAL_ROOT)).as_posix(),
                "light_preprocessed": None, "advanced_repaired": None,
                "singing_vocal_augmented": None, "other_augmented": None
            },
            "original_filename": orig_filename,
            "original_file_format": "guitar_pro_converted",
            "dataset_family": "gprotab", "dataset_type": "crawled_target_site",
            "duplicate_match_type": None, "duplicate_detected_by": None
        }],
        "external_metadata": {
            "basic_info": {"song_title": song_title, "album": None, "release_date": None, "geographic_region": None},
            "creative_credits": {
                "artist_band_name": artist_band, "songwriter": None,
                "lyricist": None, "arranger_sequencer": None, "composer": composer
            },
            "descriptive_tags": {
                "genres_styles": meta["styles"], "moods_emotions": [],
                "key_description": None, "harmony_chord_description": None, "lyrics": None
            },
            "genre_specific_metadata": {
                "classical_music": {"classical_period": None, "form": None, "work_number": None, "opus_number": None, "catalog_number": None, "movement": None, "performing_forces": None},
                "choral_music": {"performing_forces": None},
                "film_tv_anime_soundtrack": {"media_type": None, "source_work_name": None, "larger_media_series": None, "production_company_or_studio": None, "source_work_genres": [], "scene_context_and_function": None, "source_work_description": None},
                "game_music": {"game_name": None, "game_series": None, "game_company": None, "game_platform": None, "game_genres": [], "scene_context_and_function": None, "game_description": None}
            },
            "links": {"gprotab": song_url},
            "other": {
                "rating": meta["rating"], "downloads": meta["downloads"],
                "file_size": meta["file_size"], "original_tab_file_type": tab_type
            }
        },
        "multimodal_assets": {
            "text": {"abc_notation": None, "lilypond": None, "humdrum_kern": None, "mei": None, "chordpro": None, "other_text_notation": None},
            "audio": {"full_mix_audio": [], "algorithmically_synthesized_audio": [], "performance_recording_audio": [], "likely_related_audio": []},
            "vision": {
                "score_pdf": {"piano_score": None, "orchestral_full_score": None, "string_ensemble_score": None, "horn_section_score": None, "choral_score": None, "other_staff_notation_full_score": None, "other_staff_notation_instrument_part": [], "guitar_tab": None, "bass_tab": None, "drum_score": None, "lead_sheet": None, "chord_chart": None, "jianpu_score": None, "other_score": None},
                "score_image": {"piano_score": None, "orchestral_full_score": None, "string_ensemble_score": None, "horn_section_score": None, "choral_score": None, "other_staff_notation_full_score": None, "other_staff_notation_instrument_part": [], "guitar_tab": None, "bass_tab": None, "drum_score": None, "lead_sheet": None, "chord_chart": None, "jianpu_score": None, "other_score": None}
            },
            "project": {
                "daw_project": None, "notation_or_editable_score_project": None,
                "tablature_project": gp_path_proj.relative_to(Path(LOCAL_ROOT)).as_posix(), "tracker_project": None
            }
        }
    }
    
    jsonl_path = Path(LOCAL_ROOT) / "metadata" / "metadata.jsonl"
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(retrieval_metadata, ensure_ascii=False) + "\n")
        
    log_checkpoint(song_url, "success")
    return True

# --- Main Execution ---
def main():
    parser = argparse.ArgumentParser(description="GProTab Scraper to converted MIDI Pipeline")
    parser.add_argument("--discover", action="store_true", help="Run alphabet discovery phase")
    parser.add_argument("--limit", type=int, default=0, help="Process max N tabs (0 = unlimited)")
    parser.add_argument("--mscore-path", type=str, default="", help="Path to MuseScore executable")
    args = parser.parse_args()
    
    setup_dirs()
    musescore_bin = args.mscore_path or find_musescore_executable()
    if not musescore_bin:
        print("[-] MuseScore CLI path not resolved. Please set `--mscore-path`.")
        sys.exit(1)
    else:
        print(f"[+] Using MuseScore executable path: {musescore_bin}")
        
    # Phase 1: Load or discover the items
    if args.discover:
        all_song_urls = run_discovery()
    else:
        cache_file = Path(LOCAL_ROOT) / "discovery_cache.json"
        if not cache_file.exists():
            print("[-] discovery_cache.json not found. Run with --discover first.")
            sys.exit(1)
            
        with open(cache_file, "r", encoding="utf-8") as f:
            all_song_urls = json.load(f)
            
    # Phase 2: Filter out completed URLs via checkpoint log
    processed_urls = load_processed_urls()
    pending_urls = [u for u in all_song_urls if u not in processed_urls]
    
    print(f"[+] Total URLs in cache: {len(all_song_urls)}")
    print(f"[+] Already processed:   {len(processed_urls)}")
    print(f"[+] Remaining to fetch:  {len(pending_urls)}")
    
    if args.limit > 0:
        pending_urls = pending_urls[:args.limit]
        print(f"[+] Applying limit. Will process: {len(pending_urls)}")
        
    # By using the length of processed URLs as the piece_index, 
    # the 500-folder batches will pick up exactly where they left off!
    piece_index = len(processed_urls)
    success_count = 0
    
    try:
        for url in pending_urls:
            success = scrape_song(url, piece_index, musescore_bin)
            if success:
                success_count += 1
                piece_index += 1
            time.sleep(1.5)
    except KeyboardInterrupt:
        print("\n[!] Execution paused by user. Progress is saved in checkpoint.jsonl.")
        print("[!] You can safely restart this script at any time.")
        sys.exit(0)
        
    print(f"\n[+] Completed execution loop. Successfully processed {success_count} tracks.")

if __name__ == "__main__":
    main()