from app import app

# 这会触发app.py中的数据库初始化代码
with app.app_context():
    pass  # 空操作，只是为了触发上下文
