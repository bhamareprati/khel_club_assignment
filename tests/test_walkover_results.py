"""How walkovers appear in standings, fixtures and exports (and that regular results do not change)."""
import csv
import io

from conftest import walkover
from models import db, Match, Round
import services.match_outcomes as outcomes


def finalize(client, world, match, score):
    r = client.post('/update-score', json={'match_id': match.id, 'tournament_id': world.tid,
                                           'score': score, 'final': True})
    assert r.status_code == 200
    return r


def standings_row(payload, round_id, pool, team_id):
    return next(t for t in payload[round_id]['pools'][pool] if t['team_id'] == team_id)


# --- Test 10: pool standings ---------------------------------------------------

def test_pool_standings_credit_walkover_win_without_points(client, world):
    for t in 'ABC':
        world.team(t)
    m_wo = world.match('A', 'B')            # walkover, A wins
    m_reg = world.match('A', 'C', status='on-going')
    m_reg2 = world.match('B', 'C', status='on-going')
    walkover(client, m_wo.id, world.tid, 'A')
    finalize(client, world, m_reg, '11-7')
    finalize(client, world, m_reg2, '3-11')

    data = client.get(f'/standings/{world.tid}').get_json()
    a, b, c = (standings_row(data, '1', 'A', t) for t in 'ABC')

    # A: walkover win + regular win (11-7); the walkover adds no points
    assert (a['matches_played'], a['matches_won'], a['matches_lost']) == (2, 2, 0)
    assert (a['points_scored'], a['points_lost'], a['points_difference']) == (11, 7, 4)
    assert a['total_scores'] == 4
    # B: walkover loss + regular loss (3-11); NOT a 0-0 draw and no win credited
    assert (b['matches_played'], b['matches_won'], b['matches_lost']) == (2, 0, 2)
    assert (b['points_scored'], b['points_lost']) == (3, 11)
    # C: regular only
    assert (c['matches_played'], c['matches_won']) == (2, 1)


def test_walkover_only_pool_has_zero_point_difference(client, world):
    world.team('A'); world.team('B')
    m = world.match('A', 'B')
    walkover(client, m.id, world.tid, 'B')
    data = client.get(f'/standings/{world.tid}').get_json()
    a, b = standings_row(data, '1', 'A', 'A'), standings_row(data, '1', 'A', 'B')
    assert (b['matches_played'], b['matches_won'], b['points_difference']) == (1, 1, 0)
    assert (a['matches_played'], a['matches_won'], a['matches_lost'], a['points_difference']) == (1, 0, 1, 0)
    assert data['1']['pools']['A'][0]['team_id'] == 'B'     # winner ranks first


def test_unfinished_zero_zero_matches_still_not_counted(client, world):
    """Legacy rule preserved: a match that was never played does not count."""
    world.team('A'); world.team('B')
    world.match('A', 'B')
    data = client.get(f'/standings/{world.tid}').get_json()
    a = standings_row(data, '1', 'A', 'A')
    assert (a['matches_played'], a['matches_won']) == (0, 0)


def test_overall_and_second_place_standings_include_walkover(client, world):
    for t in 'ABC':
        world.team(t)
    walkover(client, world.match('A', 'B').id, world.tid, 'A')
    finalize(client, world, world.match('A', 'C', status='on-going'), '11-9')

    overall = client.get(f'/overall-standings/{world.tid}').get_json()['standings']
    rows = {r['team_id']: r for r in overall}
    assert (rows['A']['matches_played'], rows['A']['matches_won'], rows['A']['points_difference']) == (2, 2, 2)
    assert (rows['B']['matches_played'], rows['B']['matches_won'], rows['B']['points_difference']) == (1, 0, 0)

    second = client.get(f'/second-place-standings/{world.tid}')
    assert second.status_code == 200
    assert second.get_json()['standings'][0]['team_id'] in {'B', 'C'}   # A is first place


def test_round_helper_standings_credit_walkover_once(client, world):
    from routes.round.round_completion import get_round_standings
    from routes.round.round_knockout import get_pool_standings
    for t in 'AB':
        world.team(t)
        db.session.add(Round(round_id=1, team_id=t, pool='A', tournament_id=world.tid, name='R1'))
    db.session.commit()
    m = world.match('A', 'B')
    walkover(client, m.id, world.tid, 'A')

    pools = get_round_standings(world.tid, '1')['1']['pools']['A']
    rows = {r['team_id']: r for r in pools}
    assert (rows['A']['matches_won'], rows['A']['points_scored'], rows['A']['points_difference']) == (1, 0, 0)
    assert (rows['B']['matches_won'], rows['B']['points_lost']) == (0, 0)
    assert len(rows['A']['matches_played']) == 1

    ko = get_pool_standings(world.tid, 1)['A']
    assert (ko['A']['matches_played'], ko['A']['matches_won'], ko['A']['points_scored']) == (1, 1, 0)
    assert (ko['B']['matches_played'], ko['B']['matches_won']) == (1, 0)


def test_duplicated_score_rows_do_not_double_count_walkover(client, world):
    from models import Score
    from routes.round.round_completion import get_round_standings
    world.team('A'); world.team('B')
    m = world.match('A', 'B')
    walkover(client, m.id, world.tid, 'A')
    db.session.add(Score(match_id=m.id, team_id='A', score=0, tournament_id=world.tid))  # legacy duplicate row
    db.session.commit()
    rows = {r['team_id']: r for r in get_round_standings(world.tid, '1')['1']['pools']['A']}
    assert rows['A']['matches_won'] == 1


# --- Fixtures / serializers ----------------------------------------------------

def test_fixtures_label_walkover_and_keep_regular_results(client, world):
    for t in 'ABCD':
        world.team(t)
    m_wo = world.match('A', 'B')
    m_reg = world.match('C', 'D', status='on-going')
    m_pending = world.match('A', 'C')
    walkover(client, m_wo.id, world.tid, 'B')
    finalize(client, world, m_reg, '11-7')

    r = client.get('/get-match-fixtures', query_string={'tournament_id': world.tid})
    assert r.status_code == 200, r.get_data(as_text=True)
    by_id = {m['match_id']: m for m in r.get_json()['matches']}

    wo = by_id[m_wo.id]
    assert wo['match_result'] == 'Walkover'
    assert wo['match_status'] == {'winner_team_id': 'B', 'is_final': True,
                                  'status': 'completed', 'outcome_type': 'walkover'}
    reg = by_id[m_reg.id]
    assert reg['match_result'] == '11-7'
    assert reg['match_status']['outcome_type'] == 'regular' and reg['match_status']['winner_team_id'] == 'C'
    pending = by_id[m_pending.id]
    assert pending['match_result'] == '0-0' and pending['match_status']['outcome_type'] is None
    # untouched keys
    assert {'match_name', 'round_id', 'pool', 'team1', 'team2', 'team1_players', 'bracket_info'} <= set(wo)


def test_fixtures_csv_and_dupr_export(client, world):
    for t in 'ABCD':
        world.team(t)
    m_wo = world.match('A', 'B')
    m_reg = world.match('C', 'D', status='on-going')
    walkover(client, m_wo.id, world.tid, 'A')
    finalize(client, world, m_reg, '11-7')

    r = client.get('/get-match-fixtures/csv', query_string={'tournament_id': world.tid})
    rows = list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
    results = {int(row['Match ID']): row['Result'] for row in rows}
    assert results[m_wo.id] == 'Walkover' and results[m_reg.id] == '11-7'

    # DUPR report: only matches that were actually played are exported
    r = client.get('/export-tournament-csv', query_string={'tournament_id': world.tid})
    assert r.status_code == 200
    data_rows = list(csv.reader(io.StringIO(r.get_data(as_text=True))))[1:]
    assert len(data_rows) == 1


def test_match_ops_pool_fixtures_expose_outcome(client, world):
    from models import Round
    for t in 'AB':
        world.team(t)
        db.session.add(Round(round_id=1, team_id=t, pool='A', tournament_id=world.tid, name='R1'))
    db.session.commit()
    m = world.match('A', 'B')
    walkover(client, m.id, world.tid, 'A')
    r = client.get('/match-ops/pools/A/fixtures', query_string={'tournament_id': world.tid})
    assert r.status_code == 200, r.get_data(as_text=True)
    match = r.get_json()['matches'][0]
    assert (match['outcome_type'], match['winner_team_id'], match['is_final']) == ('walkover', 'A', True)


# --- Registry / extensibility ----------------------------------------------------

def test_registry_helpers():
    assert outcomes.uses_scores(None) and outcomes.uses_scores('regular')
    assert not outcomes.uses_scores('walkover')
    assert outcomes.result_label('walkover', 0, 0) == 'Walkover'
    assert outcomes.result_label(None, 11, 7) == '11-7'
    d = outcomes.decided_result('walkover', 'A', 'B', 'B')
    assert (d.winner_team_id, d.loser_team_id) == ('B', 'A')
    assert outcomes.decided_result('regular', 'A', 'B', 'B') is None
    assert outcomes.decided_result('walkover', 'A', 'B', None) is None
    assert outcomes.decided_result('walkover', 'A', 'B', 'Z') is None


def test_adding_a_new_declarable_outcome_needs_only_a_registry_entry(client, world, monkeypatch):
    """The extensibility claim: a hypothetical 'forfeit' works end to end with one registry entry."""
    forfeit = outcomes.OutcomeDefinition('forfeit', 'Forfeit', uses_scores=False,
                                         declarable=True, requires_winner=True)
    monkeypatch.setitem(outcomes.OUTCOMES, 'forfeit', forfeit)

    for t in 'AB':
        world.team(t)
    m = world.match('A', 'B')
    r = client.post(f'/matches/{m.id}/outcome', json={'tournament_id': world.tid,
                                                     'outcome_type': 'forfeit', 'winner_team_id': 'A'})
    assert r.status_code == 200 and r.get_json()['message'] == 'Forfeit recorded successfully'
    # labelled in fixtures, counted by standings, locked against score edits
    fx = client.get('/get-match-fixtures', query_string={'tournament_id': world.tid}).get_json()['matches'][0]
    assert fx['match_result'] == 'Forfeit'
    st = client.get(f'/standings/{world.tid}').get_json()
    assert standings_row(st, '1', 'A', 'A')['matches_won'] == 1
    assert client.post('/update-score', json={'match_id': m.id, 'tournament_id': world.tid,
                                              'score': '1-0'}).status_code == 409
