FROM nikolaik/python-nodejs:python3.11-nodejs18

# Install required OS dependencies for Firefox/Camoufox via Playwright's helper and Xvfb
RUN apt-get update && apt-get install -y xvfb \
    && npm install -g playwright && npx playwright install-deps

WORKDIR /app
COPY . /app

# Install Python dependencies
RUN pip install -r requirements.txt

# Install Node dependencies for the server
RUN cd camofox-browser && npm install

# Start the services
CMD ["bash", "start.sh"]
