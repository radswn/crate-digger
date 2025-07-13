import spotipy
from spotipy.oauth2 import SpotifyOAuth

from dotenv import load_dotenv


scope = "user-library-read"
load_dotenv()

auth = SpotifyOAuth(scope=scope)
sp = spotipy.Spotify(auth_manager=auth)

results = sp.current_user_saved_tracks()
for idx, item in enumerate(results['items']):
    track = item['track']
    print(idx, track['artists'][0]['name'], " – ", track['name'])
