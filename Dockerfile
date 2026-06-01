FROM nikolaik/python-nodejs:python3.11-nodejs18

# Install required OS dependencies for Firefox/Camoufox
RUN apt-get update && apt-get install -y \
    libgtk-3-0 libx11-xcb1 libdbus-glib-1-2 libxt6 libpci-dev xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Install Python dependencies
RUN pip install -r requirements.txt

# Install Node dependencies for the server
RUN cd camofox-browser && npm install

# Start the services
CMD ["bash", "start.sh"]
