# Lucky-7 Scraper

A Python-based web scraper for Lucky 7 card game data. This tool automates the process of collecting card results from online Lucky 7 games and saves them to a CSV file for analysis.

## Features

- Automatically logs into the target site
- Navigates to the Lucky 7 game section
- Scrapes card data from the game interface
- Parses card rank and suit information
- Saves results to CSV with timestamps
- Handles game refreshes and timeouts
- Clean terminal output with minimal logging

## Installation

1. Clone this repository
2. Install the required dependencies:

```bash
pip install -r requirements.txt
```

## Usage

Run the scraper with:

```bash
python scraper.py
```

To stop the scraper, press `Ctrl+C`.

## Data Format

The scraper saves data to `lucky7_data.csv` with the following columns:

- `ts_utc`: Timestamp in UTC
- `round_id`: Game round identifier (if available)
- `rank`: Card rank (1-13, where 1=Ace, 11=Jack, 12=Queen, 13=King)
- `suit_key`: Card suit (S=Spades, H=Hearts, D=Diamonds, C=Clubs)
- `color`: Card color (red or black)
- Additional derived features

## Requirements

- Python 3.6+
- Selenium
- BeautifulSoup4
- webdriver-manager

## License

MIT