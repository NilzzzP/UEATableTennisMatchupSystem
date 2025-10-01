import csv
import os
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

# Initialize the Flask application
app = Flask(__name__, static_folder='.', static_url_path='', template_folder='.')
CORS(app)  # Enable Cross-Origin Resource Sharing

# --- Constants ---
PLAYERS_CSV_FILE = 'players.csv'
CSV_HEADERS = ['id', 'name', 'elo', 'wins', 'losses', 'is_playing']
STARTING_ELO = 1000

# --- State Management (in-memory) ---
session_data = {
    "is_active": False,
    "players": {},
    "waiting_ids": [],
    "active_matches": [],
    "max_tables": 0
}

# --- Helper Functions ---
def get_players_from_csv():
    """Reads all players from the CSV file."""
    if not os.path.exists(PLAYERS_CSV_FILE):
        return []
    try:
        # FIX: Specify UTF-8 encoding with BOM support to handle files saved by different editors (like Excel)
        with open(PLAYERS_CSV_FILE, 'r', newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            players = []
            for row in reader:
                if row and 'elo' in row and row['elo'] is not None and row['elo'] != '':
                    try:
                        row['elo'] = int(float(row['elo']))
                        row['wins'] = int(row['wins'])
                        row['losses'] = int(row['losses'])
                        row['is_playing'] = row['is_playing'].lower() == 'true'
                        players.append(row)
                    except (ValueError, KeyError) as e:
                        print(f"Skipping malformed row: {row}. Error: {e}")
            return players
    except (IOError, csv.Error) as e:
        print(f"Error reading CSV file: {e}")
        return []

def write_players_to_csv(players):
    """Writes a list of player dictionaries to the CSV file."""
    try:
        with open(PLAYERS_CSV_FILE, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
            players_to_write = [p.copy() for p in players]
            for p in players_to_write:
                p['is_playing'] = str(p['is_playing'])
            writer.writerows(players_to_write)
    except IOError as e:
        print(f"Error writing to CSV file: {e}")

def get_next_player_id(players):
    """Generates a new unique player ID."""
    if not players:
        return "1"
    return str(max(int(p['id']) for p in players) + 1)

# --- ELO Calculation Logic ---
def calculate_new_ratings(winner_rating, loser_rating, winner_score, loser_score):
    """Calculates ELO change based on a best-of-3 match result."""
    K_BASE = 32
    k_multiplier = 1.0
    if winner_score == 2 and loser_score == 0:
        k_multiplier = 1.5
    
    K = K_BASE * k_multiplier
    prob_winner = 1 / (1 + 10**((loser_rating - winner_rating) / 400))
    rating_change = K * (1 - prob_winner)
    
    return winner_rating + rating_change, loser_rating - rating_change

# --- API Endpoints ---
@app.route('/')
def index():
    """Serves the main HTML file."""
    return render_template('table--matcher.html')

@app.route('/api/session', methods=['GET'])
def get_session():
    """Endpoint to get the current session state."""
    return jsonify(get_session_state())

@app.route('/api/players', methods=['GET'])
def get_players():
    """Endpoint to get the list of all players."""
    return jsonify(get_players_from_csv())

@app.route('/api/players', methods=['POST'])
def add_player():
    """Endpoint to add a new player. Now works mid-session."""
    data = request.json
    if not data or 'name' not in data or not data['name'].strip():
        return jsonify({"error": "Player name is required"}), 400
    
    players = get_players_from_csv()
    new_player = {
        'id': get_next_player_id(players),
        'name': data['name'].strip(),
        'elo': STARTING_ELO,
        'wins': 0,
        'losses': 0,
        'is_playing': True
    }
    players.append(new_player)
    write_players_to_csv(players)

    if session_data['is_active']:
        session_data['players'][new_player['id']] = new_player
        session_data['waiting_ids'].append(new_player['id'])
        session_data['waiting_ids'].sort(key=lambda pid: session_data['players'][pid]['elo'], reverse=True)
        fill_empty_tables()
    
    return jsonify(new_player), 201

@app.route('/api/players/<player_id>', methods=['DELETE'])
def delete_player(player_id):
    """Endpoint to delete a player."""
    players = get_players_from_csv()
    players_to_keep = [p for p in players if p['id'] != player_id]
    
    if len(players) == len(players_to_keep):
        return jsonify({"error": "Player not found"}), 404
        
    write_players_to_csv(players_to_keep)
    return jsonify({"message": "Player deleted"}), 200

@app.route('/api/players/toggle', methods=['POST'])
def toggle_player_status():
    """Endpoint to toggle a player's is_playing status. Now works mid-session."""
    data = request.json
    player_id = str(data.get('id'))
    
    players = get_players_from_csv()
    player_found = False
    for p in players:
        if p['id'] == player_id:
            p['is_playing'] = not p['is_playing']
            player_found = True
            
            if session_data['is_active'] and not p['is_playing']:
                if player_id in session_data['players']:
                    del session_data['players'][player_id]
                session_data['waiting_ids'] = [pid for pid in session_data['waiting_ids'] if pid != player_id]
            break
            
    if not player_found:
        return jsonify({"error": "Player not found"}), 404

    write_players_to_csv(players)
    return jsonify({"message": "Player status updated"}), 200

@app.route('/api/session/start', methods=['POST'])
def start_session():
    """Endpoint to start a new match session."""
    data = request.json
    session_data['max_tables'] = data.get('tableCount', 1)
    
    all_players = get_players_from_csv()
    players_for_session = [p for p in all_players if p['is_playing']]
    
    if len(players_for_session) < 2:
        return jsonify({"error": "Need at least 2 active players"}), 400

    session_data['is_active'] = True
    session_data['players'] = {p['id']: p for p in players_for_session}
    session_data['waiting_ids'] = sorted([p['id'] for p in players_for_session], key=lambda pid: session_data['players'][pid]['elo'], reverse=True)
    session_data['active_matches'] = []
    
    fill_empty_tables()
    return jsonify(get_session_state())

def fill_empty_tables():
    """Internal logic to create new matches from the queue."""
    while len(session_data['active_matches']) < session_data['max_tables'] and len(session_data['waiting_ids']) >= 2:
        p1_id = session_data['waiting_ids'].pop(0)
        p2_id = session_data['waiting_ids'].pop(0)
        session_data['active_matches'].append({
            "id": f"match-{p1_id}-{p2_id}",
            "player1Id": p1_id,
            "player2Id": p2_id
        })

@app.route('/api/session/end', methods=['POST'])
def end_session():
    """Endpoint to end the current session."""
    session_data.update({
        "is_active": False, "players": {}, "waiting_ids": [], 
        "active_matches": [], "max_tables": 0
    })
    return jsonify({"message": "Session ended"})

@app.route('/api/session/record', methods=['POST'])
def record_result():
    """Endpoint to record a match result and generate a new match."""
    data = request.json
    winner_id = str(data['winnerId'])
    loser_id = str(data['loserId'])

    all_players = get_players_from_csv()
    winner = next((p for p in all_players if p['id'] == winner_id), None)
    loser = next((p for p in all_players if p['id'] == loser_id), None)

    if not winner or not loser:
        return jsonify({"error": "Winner or loser not found in master list"}), 404

    new_winner_rating, new_loser_rating = calculate_new_ratings(
        winner['elo'], loser['elo'], data['winnerScore'], data['loserScore']
    )
    winner['elo'] = round(new_winner_rating)
    winner['wins'] += 1
    loser['elo'] = round(new_loser_rating)
    loser['losses'] += 1
    write_players_to_csv(all_players)

    if session_data['is_active']:
        session_data['players'][winner_id]['elo'] = winner['elo']
        session_data['players'][winner_id]['wins'] += 1
        session_data['players'][loser_id]['elo'] = loser['elo']
        session_data['players'][loser_id]['losses'] += 1
        
        session_data['active_matches'] = [
            m for m in session_data['active_matches'] 
            if not ((m['player1Id'] == winner_id and m['player2Id'] == loser_id) or 
                    (m['player1Id'] == loser_id and m['player2Id'] == winner_id))
        ]

        session_data['waiting_ids'].extend([winner_id, loser_id])
        session_data['waiting_ids'].sort(key=lambda pid: session_data['players'][pid]['elo'], reverse=True)
        
        fill_empty_tables()
    return jsonify(get_session_state())

def get_session_state():
    """Constructs the current session state to send to the frontend."""
    return {
        "isActive": session_data["is_active"],
        "activeMatches": [{
            **match,
            'player1': session_data['players'].get(match['player1Id']),
            'player2': session_data['players'].get(match['player2Id']),
        } for match in session_data['active_matches']],
        "waitingPlayers": [session_data['players'][pid] for pid in session_data['waiting_ids']]
    }

if __name__ == '__main__':
    if not os.path.exists(PLAYERS_CSV_FILE):
        with open(PLAYERS_CSV_FILE, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
    
    app.run(debug=True, port=5001)

