from flask import Flask
from routes import main
from api import api

app = Flask(__name__)
app.register_blueprint(main)
app.register_blueprint(api)

if __name__ == "__main__":
    app.run(debug=True)
