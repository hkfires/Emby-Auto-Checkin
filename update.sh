docker compose down
git pull
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
git reset --hard origin/$CURRENT_BRANCH
docker compose up --build -d
