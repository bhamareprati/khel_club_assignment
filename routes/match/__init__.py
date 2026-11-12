from flask import Blueprint

match_bp = Blueprint('match', __name__)

# Import specific functions from each module
from .match_core import (
    create_match, check_player_checkins, update_checkin_status,
    assign_pool, assign_court_and_pool
)
from .match_fixtures import get_match_fixtures, get_match_fixtures_csv
from .match_outcome import declare_match_outcome
from .match_pools import get_pools, update_pools 