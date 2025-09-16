# convert_avatars.py
import os
from PIL import Image
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

# 初始化Flask应用
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///site.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# 用户模型（与app.py中一致）
class UserModel(db.Model):
    __tablename__ = 'user_model'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False)
    avatar = db.Column(db.String(100), default='/static/images/avatars/default.jpg')
    # 其他字段省略...

def convert_avatar_to_webp():
    avatars_dir = 'static/images/avatars'
    
    with app.app_context():
        # 遍历avatars目录
        for filename in os.listdir(avatars_dir):
            if filename.lower().endswith('.png'):
                # 构建完整文件路径
                png_path = os.path.join(avatars_dir, filename)
                
                # 读取PNG图片
                try:
                    img = Image.open(png_path)
                    
                    # 生成新的webp文件名
                    webp_filename = os.path.splitext(filename)[0] + '.webp'
                    webp_path = os.path.join(avatars_dir, webp_filename)
                    
                    # 转换为webp格式并保存
                    img.save(webp_path, 'WEBP', quality=80)
                    
                    # 更新数据库中的头像路径
                    username = filename.split('_')[0]
                    user = UserModel.query.filter_by(username=username).first()
                    if user:
                        user.avatar = f'/static/images/avatars/{webp_filename}'
                        db.session.commit()
                        print(f'Updated avatar path for {username}: {webp_filename}')
                    
                    # 删除原始png文件
                    os.remove(png_path)
                    print(f'Converted {filename} to {webp_filename}')
                    
                except Exception as e:
                    print(f'Error processing {filename}: {str(e)}')

if __name__ == '__main__':
    print('Starting avatar conversion...')
    convert_avatar_to_webp()
    print('Conversion completed!')
