import requests
import pandas as pd
from datetime import datetime, timedelta
import logging
from bs4 import BeautifulSoup
from config.settings import BSE_API_BASE_URL, MAX_REQUESTS_PER_MINUTE
from database.db_manager import DatabaseManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class BSEScraper:
    """Scrapes data from Bombay Stock Exchange (BSE)"""

    def __init__(self):
        self.base_url = BSE_API_BASE_URL
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.bseindia.com',
        })
        self.db = DatabaseManager()

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def fetch_stock_list(self):
        """Fetch list of equity stocks listed on BSE.

        BSE exposes a CSV/JSON endpoint that returns all scrip codes along
        with company names and segment info.  The response is a list of dicts
        with at least 'scrip_code' and 'scrip_name' keys.
        """
        try:
            # BSE publishes a downloadable equity list; the endpoint below
            # returns JSON via their public API.
            url = f'{self.base_url}getScripHeaderData'
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            stocks = data.get('Table', [])
            logger.info(f"Fetched {len(stocks)} stocks from BSE")
            return stocks
        except Exception as e:
            logger.error(f"Error fetching stock list: {str(e)}")
            return []

    def fetch_daily_data(self, scrip_codes, date=None):
        """Fetch daily OHLCV data for given BSE scrip codes.

        Parameters
        ----------
        scrip_codes : list[str]
            BSE scrip codes (6-digit numeric identifiers, e.g. '500325').
        date : str | None
            Target date in 'DD/MM/YYYY' format.  Defaults to today.
        """
        if date is None:
            date = datetime.now().strftime('%d/%m/%Y')

        results = []
        for scrip_code in scrip_codes:
            try:
                data = self._fetch_scrip_data(scrip_code, date)
                if data:
                    results.append(data)
                    self._save_to_database(scrip_code, data)
            except Exception as e:
                logger.error(f"Error fetching data for {scrip_code}: {str(e)}")

        return results

    def fetch_historical_data(self, scrip_code, start_date, end_date):
        """Fetch historical OHLCV data for a scrip.

        Parameters
        ----------
        scrip_code : str
            BSE scrip code.
        start_date : str
            Start date in 'DD/MM/YYYY' format.
        end_date : str
            End date in 'DD/MM/YYYY' format.
        """
        try:
            url = (
                f'{self.base_url}getbhavdata'
                f'?scrip_cd={scrip_code}&from={start_date}&to={end_date}'
            )
            response = self.session.get(url, timeout=20)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(
                f"Error fetching historical data for {scrip_code}: {str(e)}"
            )
            return None

    def fetch_corporate_actions(self, scrip_code):
        """Fetch corporate actions (dividends, splits, bonuses, etc.) for a scrip."""
        try:
            url = f'{self.base_url}corporateActions?scrip_cd={scrip_code}'
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(
                f"Error fetching corporate actions for {scrip_code}: {str(e)}"
            )
            return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_scrip_data(self, scrip_code, date):
        """Fetch quote data for a single BSE scrip code.

        BSE's quote endpoint returns a JSON payload whose 'Body' key contains
        price information for the requested scrip.
        """
        try:
            url = f'{self.base_url}getQuotes?scrip_cd={scrip_code}'
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()

            body = data.get('Body', [])
            if not body:
                logger.warning(f"No data returned for scrip {scrip_code}")
                return None

            quote = body[0]  # first (and typically only) record
            return {
                'scrip_code': scrip_code,
                'symbol': quote.get('scripShortName', scrip_code),
                'date': date,
                'open': float(quote.get('OpenPrice', 0) or 0),
                'high': float(quote.get('High52Week', 0) or 0),   # daily high field varies
                'low': float(quote.get('Low52Week', 0) or 0),
                'close': float(quote.get('CurrRate', 0) or 0),
                'volume': int(quote.get('TTradedQty', 0) or 0),
                'previous_close': float(quote.get('PrevRate', 0) or 0),
            }
        except Exception as e:
            logger.error(f"Error fetching data for {scrip_code}: {str(e)}")
            return None

    def _save_to_database(self, scrip_code, data):
        """Persist fetched quote data into the shared database.

        Mirrors the NSE scraper's upsert logic; uses 'symbol' as the lookup
        key so both scrapers share the same 'stocks' and 'price_history' tables.
        """
        try:
            symbol = data['symbol']

            # Resolve or create the stock record
            stock_query = 'SELECT stock_id FROM stocks WHERE symbol = %s'
            result = self.db.execute_query(stock_query, (symbol,))

            if not result:
                insert_stock = (
                    'INSERT INTO stocks (symbol, company_name, scrip_code, exchange) '
                    'VALUES (%s, %s, %s, %s)'
                )
                stock_id = self.db.execute_insert(
                    insert_stock, (symbol, symbol, scrip_code, 'BSE')
                )
            else:
                stock_id = result[0][0]

            # Upsert daily price row
            price_query = '''
                INSERT INTO price_history
                    (stock_id, price_date, open_price, high_price, low_price,
                     close_price, volume, exchange)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    open_price  = VALUES(open_price),
                    high_price  = VALUES(high_price),
                    low_price   = VALUES(low_price),
                    close_price = VALUES(close_price),
                    volume      = VALUES(volume)
            '''
            self.db.execute_insert(price_query, (
                stock_id,
                data['date'],
                data['open'],
                data['high'],
                data['low'],
                data['close'],
                data['volume'],
                'BSE',
            ))

            logger.info(f"Saved BSE data for {symbol} ({scrip_code}) on {data['date']}")

        except Exception as e:
            logger.error(f"Error saving data for {scrip_code}: {str(e)}")
