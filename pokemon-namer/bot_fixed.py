import json
import logging
import time
import os
from threading import Lock

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Data storage file path
DATA_FILE = 'data.json'
lock = Lock()

# Exponential backoff function

def exponential_backoff(retries):
    delay = 1  # initial delay
    for i in range(retries):
        yield delay
        delay *= 2  # double the delay on each retry

# Function to save data to JSON file

def save_data(data):
    with lock:
        try:
            with open(DATA_FILE, 'w') as f:
                json.dump(data, f)
                logging.info("Data saved successfully.")
        except Exception as e:
            logging.error(f"Error saving data: {e}")

# Function to load data from JSON file

def load_data():
    if not os.path.exists(DATA_FILE):
        return {}  # return empty dict

    with lock:
        try:
            with open(DATA_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error loading data: {e}")
            return {}

# Function to connect to Discord with reconnection logic

def connect_to_discord():
    max_retries = 5
    for i in range(max_retries):
        try:
            # Simulated connection logic
            logging.info("Attempting to connect to Discord...")
            # connect_to_discord_api()  # Uncomment and replace with actual connection code
            logging.info("Connected to Discord!")
            return
        except Exception as e:
            logging.error(f"Connection failed: {e}. Retrying...")
            for wait_time in exponential_backoff(i + 1):
                logging.info(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)

    logging.critical("Failed to connect after multiple retries.")
    raise Exception("Unable to connect to Discord")

if __name__ == '__main__':
    data = load_data()
    # Main logic here
