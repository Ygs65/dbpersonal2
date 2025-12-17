# Cyberpunk Redis Game

Flask + Redis + Socket.IO demo with:

- Auth (single-device login)
- Click-to-earn gold with rate limiting
- Shop / Inventory
- Auction house
- Admin console with logs & announcements

## Run locally

```bash
pip install -r requirements.txt
export REDIS_HOST=...
export REDIS_PORT=...
export REDIS_USERNAME=default
export REDIS_PASSWORD=...
export ADMIN_PASSWORD=your_admin_password
python server.py
```

Then open http://localhost:5000/ for player UI, http://localhost:5000/admin for admin UI.
