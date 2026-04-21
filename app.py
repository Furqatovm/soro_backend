import os
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity, decode_token
from werkzeug.security import generate_password_hash, check_password_hash
from openai import OpenAI 
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///chat_history.db')
app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', 'dev-secret-key')
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(days=7)

db = SQLAlchemy(app)
jwt = JWTManager(app)

client = OpenAI(
    api_key=os.getenv("GEMINI_API_KEY"),
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

# --- MODELLAR ---

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    conversations = db.relationship('Conversation', backref='user', lazy=True)

class Conversation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # user_id endi nullable=True, ya'ni login qilmaganlar uchun bo'sh bo'lishi mumkin
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    messages = db.relationship('Message', backref='conversation', cascade="all, delete-orphan")

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conv_id = db.Column(db.Integer, db.ForeignKey('conversation.id'), nullable=False)
    role = db.Column(db.String(20)) 
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()

# --- AUTH YO'LLARI ---

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    if not data or not data.get('username') or not data.get('password'):
        return jsonify({"error": "Ma'lumotlar to'liq emas"}), 400
    if User.query.filter_by(username=data['username']).first():
        return jsonify({"error": "Foydalanuvchi mavjud"}), 400
    
    hashed_pw = generate_password_hash(data['password'])
    new_user = User(username=data['username'], password_hash=hashed_pw)
    db.session.add(new_user)
    db.session.commit()
    return jsonify({"message": "Ro'yxatdan o'tdingiz"}), 201

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    user = User.query.filter_by(username=data['username']).first()
    if user and check_password_hash(user.password_hash, data['password']):
        token = create_access_token(identity=str(user.id))
        return jsonify(access_token=token), 200
    return jsonify({"error": "Login yoki parol xato"}), 401

# --- CHAT MANTIQI ---

def get_current_user_id():
    """Token bo'lsa ID qaytaradi, bo'lmasa None"""
    auth_header = request.headers.get('Authorization', None)
    if auth_header and "Bearer " in auth_header:
        try:
            token = auth_header.split(" ")[1]
            data = decode_token(token)
            return int(data['sub'])
        except:
            return None
    return None

@app.route('/conversations', methods=['GET'])
def get_convs():
    uid = get_current_user_id()
    # Faqat login qilgan foydalanuvchining chatlarini yoki hamma guest chatlarni ko'rsatish mumkin
    # Bu yerda mantiq: agar login bo'lsa o'ziniki, bo'lmasa faqat umumiy guest chatlar
    if uid:
        convs = Conversation.query.filter_by(user_id=uid).order_by(Conversation.created_at.desc()).all()
    else:
        convs = Conversation.query.filter_by(user_id=None).order_by(Conversation.created_at.desc()).limit(10).all()
        
    return jsonify([{
        "id": c.id,
        "created_at": c.created_at.isoformat(),
        "last_message": Message.query.filter_by(conv_id=c.id).order_by(Message.timestamp.desc()).first().content[:40] + "..." if Message.query.filter_by(conv_id=c.id).first() else "Yangi suhbat"
    } for c in convs])

@app.route('/chat', methods=['POST'])
def chat():
    uid = get_current_user_id()
    data = request.json
    user_msg = data.get('message', '').strip()
    conv_id = data.get('conversation_id')

    if not conv_id:
        # Yangi suhbat (Login qilgan bo'lsa uid bilan, bo'lmasa None bilan)
        new_conv = Conversation(user_id=uid)
        db.session.add(new_conv)
        db.session.commit()
        conv_id = new_conv.id
    
    # Tarixni olish
    history = Message.query.filter_by(conv_id=conv_id).order_by(Message.timestamp.desc()).limit(10).all()
    history.reverse()
    
    payload = [{"role": "system", "content": "Siz foydali yordamchisiz."}]
    for m in history:
        payload.append({"role": m.role, "content": m.content})
    payload.append({"role": "user", "content": user_msg})

    # MODELLAR (O'zidek qoldi)
    models = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-pro"]
    ai_reply = None

    for model_name in models:
        try:
            response = client.chat.completions.create(model=model_name, messages=payload, timeout=25)
            ai_reply = response.choices[0].message.content
            break
        except Exception as e:
            print(f"Xato {model_name}: {e}")

    if ai_reply:
        db.session.add(Message(conv_id=conv_id, role="user", content=user_msg))
        db.session.add(Message(conv_id=conv_id, role="assistant", content=ai_reply))
        db.session.commit()
        return jsonify({"conversation_id": conv_id, "reply": ai_reply})
    
    return jsonify({"error": "AI javob bermadi"}), 503

@app.route('/history/<int:cid>', methods=['GET'])
def get_history(cid):
    messages = Message.query.filter_by(conv_id=cid).order_by(Message.timestamp.asc()).all()
    return jsonify([{"role": m.role, "content": m.content} for m in messages])

if __name__ == '__main__':
    app.run(debug=True, port=5000)