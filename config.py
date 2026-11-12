from os import environ
from dotenv import load_dotenv

load_dotenv()

class Config:
    DB_HOST = environ.get('DB_HOST')
    DB_USER = environ.get('DB_USER')
    DB_PASSWORD = environ.get('DB_PASSWORD')
    DB_NAME = environ.get('DB_NAME')
    DB_PORT = environ.get('DB_PORT', '3306')
    
    # DATABASE_URL (optional) overrides the MySQL URI. Used for isolated local
    # development / automated tests (e.g. sqlite:///dev.db). Unset = unchanged.
    SQLALCHEMY_DATABASE_URI = environ.get('DATABASE_URL') or f'mysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}'
    SQLALCHEMY_TRACK_MODIFICATIONS = False