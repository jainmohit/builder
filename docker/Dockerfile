FROM node:22-slim

# Pre-install dependencies once, into a cache inside the image
WORKDIR /opt/cache
COPY templates/base/package.json templates/base/package-lock.json ./
RUN npm ci

WORKDIR /app
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["entrypoint.sh"]
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]