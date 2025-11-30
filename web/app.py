from flask import Flask, render_template, request, redirect, url_for, jsonify
from pymongo import MongoClient
import os
import random
import re
from datetime import datetime
from bson.objectid import ObjectId

app = Flask(__name__)

# Database Connection
# Prioritize environment variable for Docker, fallback to localhost
MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://localhost:27017/music_vault')
client = MongoClient(MONGO_URI)
db = client.music_vault
collection = db.tracks

# ================= Homepage Dashboard =================
@app.route('/')
def index():
    # 1. Top 5 Artists
    pipeline_artists = [
        {"$unwind": "$artists"},
        {"$group": {
            "_id": "$artists.name",
            "count": {"$sum": 1},
            "images": {"$addToSet": {"$arrayElemAt": ["$album.images.url", 1]}} 
        }},
        {"$project": {
            "name": "$_id",
            "count": 1,
            "artist_imgs": {"$slice": ["$images", 2]} 
        }},
        {"$sort": {"count": -1}},
        {"$limit": 5}
    ]
    top_artists = list(collection.aggregate(pipeline_artists))

    # 2. Big Year (Query year and all album covers from that year)
    pipeline_year = [
        {"$project": {
            "year": {"$substr": ["$album.release_date", 0, 4]},
            "image": {"$arrayElemAt": ["$album.images.url", 1]} # Get medium-sized image
        }},
        {"$group": {
            "_id": "$year",
            "count": {"$sum": 1},
            "images": {"$addToSet": "$image"} # Collect all covers for this year
        }},
        {"$sort": {"count": -1}},
        {"$limit": 1}
    ]
    big_year_data = list(collection.aggregate(pipeline_year))
    big_year = "N/A"
    big_year_images = []
    
    if big_year_data:
        big_year = big_year_data[0]['_id']
        # Randomly select 10 images for scrolling effect, reuse if not enough
        imgs = big_year_data[0]['images']
        big_year_images = (imgs * 10)[:20] if imgs else []

    # 3. Total Duration
    pipeline_duration = [
        {"$group": {"_id": None, "total_ms": {"$sum": "$duration_ms"}}}
    ]
    duration_data = list(collection.aggregate(pipeline_duration))
    total_ms = duration_data[0]['total_ms'] if duration_data else 0
    total_hours = int(total_ms / (1000 * 60 * 60))
    total_minutes = int((total_ms / (1000 * 60)) % 60)

    # 4. Top Genres
    pipeline_genres = [
        {"$unwind": "$genres"},
        {"$group": {
            "_id": "$genres",
            "count": {"$sum": 1},
            "all_images": {"$addToSet": {"$arrayElemAt": ["$album.images.url", 0]}} 
        }},
        {"$sort": {"count": -1}},
        {"$limit": 4}
    ]
    top_genres = list(collection.aggregate(pipeline_genres))
    for genre in top_genres:
        genre['rep_image'] = random.choice(genre['all_images']) if genre.get('all_images') else None

    return render_template('index.html', 
                           top_artists=top_artists,
                           big_year=big_year,
                           big_year_images=big_year_images,
                           total_hours=total_hours,
                           total_minutes=total_minutes,
                           top_genres=top_genres)

# ================= Browse Page & Search =================
@app.route('/songs')
def songs():
    # Pure page rendering, search is handled via API
    pop_songs = list(collection.find().sort("popularity", -1).limit(10))
    
    pipeline_newest = [
        {"$sort": {"album.release_date": -1}},
        {"$group": {"_id": "$album.name", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$sort": {"album.release_date": -1}},
        {"$limit": 10}
    ]
    new_songs = list(collection.aggregate(pipeline_newest))

    return render_template('songs.html', pop_songs=pop_songs, new_songs=new_songs)

# Full Library Search API
# 1. New: Get top 30 genres from library (for frontend filter)
@app.route('/api/genres')
def get_all_genres():
    pipeline = [
        {"$unwind": "$genres"},
        {"$group": {"_id": "$genres", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 30} # Limit to top 30 to prevent UI clutter
    ]
    genres = list(collection.aggregate(pipeline))
    # Return plain list format: ["pop", "rock", "indie"...]
    return jsonify([g['_id'] for g in genres])

# 2. Search: search API to support advanced filtering
@app.route('/api/search')
def search_api():
    query = request.args.get('q', '')
    start_date = request.args.get('start', '')
    end_date = request.args.get('end', '')
    selected_genres = request.args.getlist('genres[]') # Get multi-select array
    
    filters = []

    # A. Fuzzy text search (if input exists)
    if query:
        filters.append({
            "$or": [
                {"name": {"$regex": query, "$options": "i"}},
                {"artists.name": {"$regex": query, "$options": "i"}},
                {"album.name": {"$regex": query, "$options": "i"}}
            ]
        })
    
    # B. Date Range Query
    # Spotify date format is usually "YYYY-MM-DD", string comparison works
    if start_date:
        filters.append({"album.release_date": {"$gte": start_date}})
    if end_date:
        filters.append({"album.release_date": {"$lte": end_date}})
        
    # C. Genre Filter (Multi-select)
    # Logic: The song's genres array must contain *at least one* of the selected genres ($in)
    if selected_genres:
        filters.append({"genres": {"$in": selected_genres}})

    # D. Combine all conditions ($and)
    final_query = {}
    if filters:
        final_query = {"$and": filters}
    
    # Execute Query
    results = list(collection.find(final_query).limit(50))
    
    # Format ObjectId
    for r in results:
        r['_id'] = str(r['_id'])
    
    return jsonify(results)

# ================= Add & Delete =================
@app.route('/add', methods=['GET', 'POST'])
def add_song():
    if request.method == 'POST':
        # Get image, use placeholder if missing
        image_url = request.form.get('image_url') or "https://picsum.photos/300"
        
        # Get date (now in full YYYY-MM-DD format)
        release_date = request.form.get('release_date')
        # Default to today if date is missing
        if not release_date:
            release_date = datetime.now().strftime("%Y-%m-%d")

        new_song = {
            "_id": request.form.get('id') or str(ObjectId()), # Auto-generate ID if empty
            "name": request.form.get('name'),
            "artists": [{"name": request.form.get('artist')}],
            "album": {
                "name": request.form.get('album'), 
                "release_date": release_date, # Store full date directly
                "images": [{"url": image_url}, {"url": image_url}, {"url": image_url}]
            },
            # Process genres: split by comma and strip whitespace
            "genres": [g.strip() for g in request.form.get('genres').split(',') if g.strip()],
            "popularity": 0,
            "duration_ms": 180000, # Default 3 minutes
            "source": "manual_entry"
        }
        
        try:
            collection.insert_one(new_song)
            return redirect(url_for('songs'))
        except Exception as e:
            return f"Error adding song: {e}"
            
    return render_template('add.html')

@app.route('/song/<song_id>/review', methods=['POST'])
def add_review(song_id):
    review_text = request.form.get('review')
    collection.update_one({"_id": song_id}, {"$set": {"my_review": review_text}})
    return redirect(url_for('songs'))

@app.route('/song/<song_id>/delete', methods=['POST'])
def delete_song(song_id):
    collection.delete_one({"_id": song_id})
    return redirect(url_for('songs'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)