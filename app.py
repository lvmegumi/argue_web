
# app.py
import os
import re
from datetime import datetime, timedelta, timezone

from flask import Flask, render_template, redirect, url_for, request, flash, g, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_migrate import Migrate
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image
import io

def utc_time():
    """返回当前UTC时间"""
    return datetime.now(timezone.utc)

def format_time(dt):
    """自定义时间格式化函数，返回相对时间描述"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = utc_time()
    diff = now - dt
    
    if diff.days > 365:
        return f"{diff.days // 365}年前"
    elif diff.days > 30:
        return f"{diff.days // 30}个月前"
    elif diff.days > 0:
        return f"{diff.days}天前"
    elif diff.seconds > 3600:
        return f"{diff.seconds // 3600}小时前"
    elif diff.seconds > 60:
        return f"{diff.seconds // 60}分钟前"
    else:
        return "刚刚"

# 创建Flask应用实例
app = Flask(__name__)

# 配置密钥 - 生产环境中应从环境变量获取
app.secret_key = os.environ.get('SECRET_KEY') or 'dev-key-change-in-production'

# 配置上传文件夹
UPLOAD_FOLDER = 'static/images/avatars'
POST_IMAGE_FOLDER = 'static/images/posts'
POST_VIDEO_FOLDER = 'static/videos/posts'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'mp4', 'mov', 'avi', 'mkv'}

# 添加一个过滤器来处理Markdown格式的图片
@app.template_filter('render_markdown_images')
def render_markdown_images(content):
    """将Markdown格式的图片链接转换为HTML图片标签"""
    if not content:
        return content
    
    # 匹配Markdown图片格式: ![alt](src)
    pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
    
    def replace_func(match):
        alt_text = match.group(1)
        img_src = match.group(2)
        return f'<img src="{img_src}" alt="{alt_text}" class="img-fluid rounded">'
    
    return re.sub(pattern, replace_func, content)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['POST_IMAGE_FOLDER'] = POST_IMAGE_FOLDER
app.config['POST_VIDEO_FOLDER'] = POST_VIDEO_FOLDER

# 确保视频上传目录存在
os.makedirs(app.config['POST_VIDEO_FOLDER'], exist_ok=True)

# 确保上传目录存在
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['POST_IMAGE_FOLDER'], exist_ok=True)

# 配置数据库
# 生产环境中应使用环境变量配置数据库URI
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL') or 'sqlite:///site.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# 生产环境配置
if not os.environ.get('FLASK_ENV') == 'development':
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
    }

db = SQLAlchemy(app)

# 初始化Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = '请先登录以访问此页面。'

# 初始化数据库迁移
migrate = Migrate(app, db)

# 注册naturaltime过滤器
app.template_filter('naturaltime')(format_time)

# 用户数据库模型
class UserModel(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    password_hash = db.Column(db.String(128), nullable=False)
    avatar = db.Column(db.String(100), default='/static/images/default_avatar.jpg')
    bio = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utc_time)
    last_seen = db.Column(db.DateTime, default=utc_time)
    histories = db.relationship('HistoryModel', backref='user', lazy='dynamic')
    post_count = db.Column(db.Integer, default=0)
    post_likes_received = db.Column(db.Integer, default=0)
    comment_likes_received = db.Column(db.Integer, default=0)
    faction = db.Column(db.String(10))  # 'pro' or 'anti'
    score = db.Column(db.Float, default=1.0)  # 用户分数，初始为1分

    def get_id(self):
        return str(self.id)
    
    def update_score(self):
        """更新用户分数"""
        # 初始分数为1分
        new_score = 1.0
        
        # 计算帖子收到的点赞和点踩
        post_interactions = (db.session.query(UserPostInteraction, UserModel)
                         .join(UserModel, UserPostInteraction.user_id == UserModel.id)
                         .join(PostModel, UserPostInteraction.post_id == PostModel.id)
                         .filter(PostModel.author_id == self.id)
                         .all())
        
        # 计算评论收到的点赞和点踩
        comment_interactions = (db.session.query(UserCommentInteraction, UserModel)
                            .join(UserModel, UserCommentInteraction.user_id == UserModel.id)
                            .join(CommentModel, UserCommentInteraction.comment_id == CommentModel.id)
                            .filter(CommentModel.author_id == self.id)
                            .all())
        
        # 处理帖子互动
        for interaction, user in post_interactions:
            if interaction.liked:
                # 点赞加权分数，基于当前用户分数调整增长速度
                user_score = max(user.score or 1.0, 1.0)
                # 使用对数函数使初始增长快，后续增长慢
                weight = user_score / (500 * (1 + (self.score or 1.0) / 10))
                new_score += weight
            elif interaction.disliked:
                # 点踩减权分数，基于当前用户分数调整减少速度
                user_score = max(user.score or 1.0, 1.0)
                # 减少的分数也基于当前分数，分数越高越难减少
                weight = user_score / (500* (1 + (self.score or 1.0) / 20))
                new_score -= weight
        
        # 处理评论互动
        for interaction, user in comment_interactions:
            if interaction.liked:
                # 点赞加权分数
                user_score = max(user.score or 1.0, 1.0)
                # 使用对数函数使初始增长快，后续增长慢
                weight = user_score / (100 * (1 + (self.score or 1.0) / 5))
                new_score += weight
            elif interaction.disliked:
                # 点踩减权分数
                user_score = max(user.score or 1.0, 1.0)
                # 减少的分数也基于当前分数，分数越高越难减少
                weight = user_score / (100 * (1 + (self.score or 1.0) / 10))
                new_score -= weight
        
        # 确保最低分数为1分，并保留两位小数
        self.score = max(new_score, 1.0)
        return self.score

# 浏览历史模型
class HistoryModel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user_model.id'), nullable=False)
    url = db.Column(db.String(255), nullable=False)
    title = db.Column(db.String(100), nullable=False)
    visited_at = db.Column(db.DateTime, default=utc_time)

# 用户-帖子互动模型
class UserPostInteraction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user_model.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('post_model.id'), nullable=False)
    liked = db.Column(db.Boolean, default=False)
    disliked = db.Column(db.Boolean, default=False)
    favorited = db.Column(db.Boolean, default=False)
    interacted_at = db.Column(db.DateTime, default=utc_time)

    user = db.relationship('UserModel', backref='post_interactions')
    post = db.relationship('PostModel', backref='user_interactions')

# 用户-评论互动模型
class UserCommentInteraction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user_model.id'), nullable=False)
    comment_id = db.Column(db.Integer, db.ForeignKey('comment_model.id'), nullable=False)
    liked = db.Column(db.Boolean, default=False)
    disliked = db.Column(db.Boolean, default=False)
    interacted_at = db.Column(db.DateTime, default=utc_time)

    user = db.relationship('UserModel', backref='comment_interactions')
    comment = db.relationship('CommentModel', backref='user_interactions')

# 用户-帖子阵营模型
class UserPostFaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user_model.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('post_model.id'), nullable=False)
    faction = db.Column(db.String(10))  # 'pro' or 'anti'
    created_at = db.Column(db.DateTime, default=utc_time)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'post_id', name='_user_post_faction_uc'),
    )

    user = db.relationship('UserModel', backref='post_factions')
    post = db.relationship('PostModel', backref='user_factions')

# 帖子模型
class PostModel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    content = db.Column(db.Text, nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey('user_model.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_time)
    updated_at = db.Column(db.DateTime, default=utc_time, onupdate=utc_time)
    tags = db.Column(db.String(200))
    view_count = db.Column(db.Integer, default=0)
    image_path = db.Column(db.String(255))
    thumbnail_path = db.Column(db.String(255))
    video_path = db.Column(db.String(255))
    like_count = db.Column(db.Integer, default=0)
    dislike_count = db.Column(db.Integer, default=0)
    favorite_count = db.Column(db.Integer, default=0)
    category_id = db.Column(db.Integer, db.ForeignKey('category_model.id'))
    
    author = db.relationship('UserModel', backref='posts')
    comments = db.relationship('CommentModel', backref='post', lazy='dynamic')
    category = db.relationship('CategoryModel', backref='posts')
    
    def get_first_image(self):
        """从内容中提取第一张图片URL"""
        if not self.content:
            return None
        
        # 查找img标签
        img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', self.content)
        if img_match:
            return img_match.group(1)
        
        # 查找图片链接
        url_match = re.search(r'(https?://[^\s]+\.(?:jpg|jpeg|png|gif|webp))', self.content)
        if url_match:
            return url_match.group(1)
        
        return None

# 分类模型
class CategoryModel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('category_model.id'))
    is_main = db.Column(db.Boolean, default=False)  # 是否是大类
    order = db.Column(db.Integer, default=0)  # 排序权重
    
    parent = db.relationship('CategoryModel', remote_side=[id], backref='children')
    
    def __repr__(self):
        return f'Category {self.name}'

# 用户收藏分类模型
class UserCategoryFavorite(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user_model.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category_model.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_time)
    
    user = db.relationship('UserModel', backref='category_favorites')
    category = db.relationship('CategoryModel', backref='user_favorites')

# 评论模型
class CommentModel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey('user_model.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('post_model.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_time)
    like_count = db.Column(db.Integer, default=0)
    dislike_count = db.Column(db.Integer, default=0)
    faction = db.Column(db.String(10))  # 'pro', 'anti', 'neutral'
    
    author = db.relationship('UserModel', backref='comments')

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(UserModel, int(user_id))

# --------------- 路由定义 ---------------
# 1. 首页路由（访问根路径/时触发）
@app.route('/')
def index():
    # 获取最近一个月的时间点
    one_month_ago = utc_time() - timedelta(days=30)
    
    # 获取当前用户收藏的分类ID列表
    favorite_category_ids = []
    if current_user.is_authenticated:
        favorite_category_ids = [fav.category_id for fav in 
                               current_user.category_favorites]
    
    # 获取查询参数
    filter_favorites = request.args.get('filter') == 'favorites'
    
    # 获取当前用户收藏的分类ID列表
    favorite_category_ids = []
    if current_user.is_authenticated:
        favorite_category_ids = [fav.category_id for fav in 
                               current_user.category_favorites]
    
    # 构建基础查询
    query = PostModel.query
    
    # 如果筛选收藏板块且用户有收藏
    if filter_favorites and favorite_category_ids:
        query = query.filter(PostModel.category_id.in_(favorite_category_ids))
    
    # 按热度排序并限制数量
    hot_posts = query.order_by(
        (PostModel.view_count*0.6 + PostModel.like_count*0.4).desc()
    ).limit(20).all()
    
    return render_template('main.html', title="热门帖子", posts=hot_posts)

# 2. 关于页路由
@app.route('/about')
def about():
    return render_template('about.html', title="关于我")

# 3. 博客页路由（模拟动态数据）
@app.route('/categories')
def categories():
    return render_template('categories.html', title="板块分类")

# 帖子互动路由
@app.route('/post/<int:post_id>/<action>', methods=['POST'])
@login_required
def post_interaction(post_id, action):
    post = PostModel.query.get_or_404(post_id)
    interaction = UserPostInteraction.query.filter_by(
        user_id=current_user.id,
        post_id=post_id
    ).first()

    if not interaction:
        interaction = UserPostInteraction(
            user_id=current_user.id,
            post_id=post_id
        )
        db.session.add(interaction)

    # 保存操作前的状态
    was_liked = interaction.liked
    was_disliked = interaction.disliked

    if action == 'like':
        if interaction.liked:
            post.like_count -= 1
            interaction.liked = False
            active = False
        else:
            post.like_count += 1
            interaction.liked = True
            if interaction.disliked:
                post.dislike_count -= 1
                interaction.disliked = False
            active = True
    elif action == 'dislike':
        if interaction.disliked:
            post.dislike_count -= 1
            interaction.disliked = False
            active = False
        else:
            post.dislike_count += 1
            interaction.disliked = True
            if interaction.liked:
                post.like_count -= 1
                interaction.liked = False
            active = True
    elif action == 'favorite':
        if interaction.favorited:
            post.favorite_count -= 1
            interaction.favorited = False
            active = False
        else:
            post.favorite_count += 1
            interaction.favorited = True
            active = True
    elif action == 'join_faction':
        # 处理加入阵营逻辑 - 使用UserPostFaction模型
        faction = request.json.get('faction')
        if faction not in ['pro', 'anti']:
            return {'error': 'Invalid faction'}, 400
            
        # 查询或创建阵营记录
        faction_interaction = UserPostFaction.query.filter_by(
            user_id=current_user.id,
            post_id=post_id
        ).first()
        
        if faction_interaction:
            # 已存在阵营记录
            if faction_interaction.faction == faction:
                # 相同阵营则取消
                db.session.delete(faction_interaction)
                active = False
            else:
                # 不同阵营则切换
                faction_interaction.faction = faction
                active = True
        else:
            # 新加入阵营
            faction_interaction = UserPostFaction(
                user_id=current_user.id,
                post_id=post_id,
                faction=faction
            )
            db.session.add(faction_interaction)
            active = True
    else:
        return {'error': 'Invalid action'}, 400

    interaction.interacted_at = utc_time()
    db.session.commit()

    # 更新帖子作者的分数（仅在点赞/点踩状态改变时）
    if action in ['like', 'dislike'] and (was_liked != interaction.liked or was_disliked != interaction.disliked):
        post_author = UserModel.query.get(post.author_id)
        if post_author:
            post_author.update_score()
            db.session.commit()

    response_data = {
        'action': action,
        'active': active,
        'count': getattr(post, f'{action}_count', 0)
    }
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return response_data, 200, {'Content-Type': 'application/json'}
    
    flash(f'已{action}帖子')
    return redirect(url_for('post', post_id=post_id))

# 分类路由
@app.route('/api/categories')
def get_categories():
    # 获取所有大类
    main_categories = CategoryModel.query.filter_by(is_main=True).order_by(CategoryModel.order).all()
    
    # 获取用户收藏的分类(如果已登录)
    user_favorites = []
    if current_user.is_authenticated:
        user_favorites = [fav.category_id for fav in current_user.category_favorites]
    
    # 构建分类树
    categories = []
    for cat in main_categories:
        category_data = {
            'id': cat.id,
            'name': cat.name,
            'is_favorite': cat.id in user_favorites,
            'children': []
        }
        
        # 获取子分类
        children = CategoryModel.query.filter_by(parent_id=cat.id).order_by(CategoryModel.order).all()
        for child in children:
            category_data['children'].append({
                'id': child.id,
                'name': child.name,
                'is_favorite': child.id in user_favorites
            })
        
        categories.append(category_data)
    
    return {'categories': categories}

@app.route('/api/category/<int:category_id>/favorite', methods=['POST'])
@login_required
def toggle_category_favorite(category_id):
    category = CategoryModel.query.get_or_404(category_id)
    
    # 检查是否已收藏
    favorite = UserCategoryFavorite.query.filter_by(
        user_id=current_user.id,
        category_id=category_id
    ).first()
    
    if favorite:
        # 取消收藏
        db.session.delete(favorite)
        action = 'removed'
    else:
        # 添加收藏
        new_favorite = UserCategoryFavorite(
            user_id=current_user.id,
            category_id=category_id
        )
        db.session.add(new_favorite)
        action = 'added'
    
    db.session.commit()
    return {'status': 'success', 'action': action}

# 评论互动路由
@app.route('/comment/<int:comment_id>/<action>', methods=['POST'])
@login_required
def comment_interaction(comment_id, action):
    comment = CommentModel.query.get_or_404(comment_id)
    interaction = UserCommentInteraction.query.filter_by(
        user_id=current_user.id,
        comment_id=comment_id
    ).first()

    if not interaction:
        interaction = UserCommentInteraction(
            user_id=current_user.id,
            comment_id=comment_id
        )
        db.session.add(interaction)

    # 保存操作前的状态
    was_liked = interaction.liked
    was_disliked = interaction.disliked

    if action == 'like':
        if interaction.liked:
            comment.like_count -= 1
            interaction.liked = False
            active = False
        else:
            comment.like_count += 1
            interaction.liked = True
            if interaction.disliked:
                comment.dislike_count -= 1
                interaction.disliked = False
            active = True
    elif action == 'dislike':
        if interaction.disliked:
            comment.dislike_count -= 1
            interaction.disliked = False
            active = False
        else:
            comment.dislike_count += 1
            interaction.disliked = True
            if interaction.liked:
                comment.like_count -= 1
                interaction.liked = False
            active = True

    interaction.interacted_at = utc_time()
    db.session.commit()

    # 更新评论作者的分数（仅在点赞/点踩状态改变时）
    if action in ['like', 'dislike'] and (was_liked != interaction.liked or was_disliked != interaction.disliked):
        comment_author = UserModel.query.get(comment.author_id)
        if comment_author:
            comment_author.update_score()
            db.session.commit()

    response_data = {
        'action': action,
        'active': active,
        'count': getattr(comment, f'{action}_count')
    }
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return response_data, 200, {'Content-Type': 'application/json'}
    
    flash(f'已{action}评论')
    return redirect(url_for('post', post_id=comment.post_id))

# 4. 帖子详情页路由
@app.route('/post/<int:post_id>/choose_faction/<faction>', methods=['POST'])
@login_required
def choose_faction(post_id, faction):
    if faction not in ['pro', 'anti']:
        flash('无效的阵营选择')
        return redirect(url_for('post', post_id=post_id))
    
    # 检查是否已经选择了该帖子的阵营
    existing_faction = UserPostFaction.query.filter_by(
        user_id=current_user.id,
        post_id=post_id
    ).first()
    
    if existing_faction:
        flash('您已经选择了该帖子的阵营，不可更改')
        return redirect(url_for('post', post_id=post_id))
    
    # 创建新的阵营选择记录
    new_faction = UserPostFaction(
        user_id=current_user.id,
        post_id=post_id,
        faction=faction
    )
    db.session.add(new_faction)
    db.session.commit()
    flash(f'您已成功加入{faction}方阵营')
    return redirect(url_for('post', post_id=post_id))

@app.route('/post/<int:post_id>', methods=['GET', 'POST'])
def post(post_id):
    post = PostModel.query.get_or_404(post_id)
    
    # 增加浏览量
    post.view_count += 1
    db.session.commit()
    
    # 处理评论提交
    if request.method == 'POST' and current_user.is_authenticated:
        content = request.form.get('content')
        faction = request.form.get('faction', 'neutral')
        
        # 处理论点论证表单的特殊字段
        if request.form.get('claim'):
            claim = request.form.get('claim', '').strip()
            argument = request.form.get('argument', '').strip()
            
            # 格式化论点论证内容，保持简单明了的格式
            if claim and argument:
                content = f"论点: {claim}\n\n论证: {argument}"
            elif claim:
                content = f"论点: {claim}"
            elif argument:
                content = f"论证: {argument}"
            else:
                content = ""
            
            # 获取阵营信息
            faction = request.form.get('faction', 'neutral')
        
        if not content:
            flash('评论内容不能为空')
            return redirect(url_for('post', post_id=post.id))
        
        # 验证阵营选择
        if faction in ['pro', 'anti']:
            user_faction = UserPostFaction.query.filter_by(
                user_id=current_user.id,
                post_id=post.id,
                faction=faction
            ).first()
            
            if not user_faction:
                flash('您需要先加入该阵营才能发表阵营评论')
                return redirect(url_for('post', post_id=post.id))
    
        comment = CommentModel(
            content=content,
            author_id=current_user.id,
            post_id=post.id,
            faction=faction
        )
        db.session.add(comment)
        db.session.commit()
        flash('评论发表成功')
        
        # 使用重定向防止表单重复提交
        return redirect(url_for('post', post_id=post.id, _anchor=f'comment-{comment.id}'))
    
    # 获取评论并按阵营分类
    all_comments = CommentModel.query.filter_by(post_id=post.id)\
        .order_by(CommentModel.created_at.desc()).all()
    
    # 分类评论
    pro_comments = [c for c in all_comments if c.faction == 'pro']
    anti_comments = [c for c in all_comments if c.faction == 'anti'] 
    neutral_comments = [c for c in all_comments if c.faction == 'neutral']
    
    # 获取当前用户在该帖子的阵营选择
    user_faction = None
    if current_user.is_authenticated:
        user_faction = UserPostFaction.query.filter_by(
            user_id=current_user.id,
            post_id=post.id
        ).first()
    
    return render_template('post/detail.html', 
                          title=post.title,
                          post=post,
                          comments=all_comments,
                          pro_comments=pro_comments,
                          anti_comments=anti_comments,
                          neutral_comments=neutral_comments,
                          user_faction=user_faction)

# 最新发布路由
@app.route('/posts/latest')
def latest_posts():
    # 获取查询参数
    sort = request.args.get('sort', 'time')  # 默认按时间排序
    order = request.args.get('order', 'desc')  # 默认降序
    filter_favorites = request.args.get('filter') == 'favorites'  # 是否筛选收藏板块
    
    # 构建基础查询
    query = PostModel.query
    
    # 筛选收藏板块相关帖子
    if filter_favorites and current_user.is_authenticated:
        favorite_categories = [fav.category_id for fav in current_user.category_favorites]
        query = query.filter(PostModel.category_id.in_(favorite_categories))
    
    # 排序逻辑
    if sort == 'time':
        order_by = PostModel.created_at.desc() if order == 'desc' else PostModel.created_at.asc()
    elif sort == 'favorites':
        order_by = PostModel.favorite_count.desc() if order == 'desc' else PostModel.favorite_count.asc()
    elif sort == 'views':
        order_by = PostModel.view_count.desc() if order == 'desc' else PostModel.view_count.asc()
    elif sort == 'comments':
        from sqlalchemy import func
        query = query.outerjoin(CommentModel).group_by(PostModel.id)
        order_by = func.count(CommentModel.id).desc() if order == 'desc' else func.count(CommentModel.id).asc()
    else:
        order_by = PostModel.created_at.desc()  # 默认按时间降序
    
    # 执行查询
    posts = query.order_by(order_by).all()
    
    return render_template('posts/latest.html', 
                         title="最新发布",
                         posts=posts,
                         sort=sort,
                         order=order,
                         filter_favorites=filter_favorites)

# 标签列表路由
@app.route('/tags')
def tags():
    # 获取所有标签并统计数量
    all_posts = PostModel.query.all()
    tag_counts = {}
    
    # 统计所有帖子中的标签
    for post in all_posts:
        if post.tags:
            tags = [tag.strip() for tag in post.tags.split(',')]
            for tag in tags:
                if tag:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
    
    # 将标签数据转换为列表格式
    tags_data = []
    hot_tags_list = get_hot_tags(20)  # 获取热门标签用于标记
    
    for tag_name, count in tag_counts.items():
        tags_data.append({
            'name': tag_name,
            'count': count,
            'is_hot': tag_name in hot_tags_list
        })
    
    # 根据参数排序
    sort = request.args.get('sort', 'hot')
    if sort == 'new':
        # 按最新排序，这里简化处理，按名称排序
        tags_data.sort(key=lambda x: x['name'], reverse=True)
    elif sort == 'name':
        # 按名称排序
        tags_data.sort(key=lambda x: x['name'])
    else:
        # 默认按热度排序（标签出现次数）
        tags_data.sort(key=lambda x: x['count'], reverse=True)
    
    return render_template('tags.html', 
                         title='标签列表', 
                         tags=tags_data,
                         sort=sort,
                         hot_tags=get_hot_tags(50))

# 标签详情页路由
@app.route('/tag/<tag_name>')
def tag(tag_name):
    if not tag_name:
        flash('标签名称不能为空')
        return redirect(url_for('index'))
    
    # 获取排序参数
    sort = request.args.get('sort', 'views')
    order = request.args.get('order', 'desc')
    
    # 构建查询
    query = PostModel.query.filter(PostModel.tags.like(f'%{tag_name}%'))
    
    # 排序逻辑
    if sort == 'time':
        order_by = PostModel.created_at.desc() if order == 'desc' else PostModel.created_at.asc()
    elif sort == 'likes':
        order_by = PostModel.like_count.desc() if order == 'desc' else PostModel.like_count.asc()
    elif sort == 'favorites':
        order_by = PostModel.favorite_count.desc() if order == 'desc' else PostModel.favorite_count.asc()
    elif sort == 'comments':
        from sqlalchemy import func
        query = query.outerjoin(CommentModel).group_by(PostModel.id)
        order_by = func.count(CommentModel.id).desc() if order == 'desc' else func.count(CommentModel.id).asc()
    else:  # views
        order_by = PostModel.view_count.desc() if order == 'desc' else PostModel.view_count.asc()
    
    # 执行查询
    posts = query.order_by(order_by).all()
    
    if not posts:
        flash(f'没有找到与"{tag_name}"相关的帖子')
    
    return render_template('tag/generic.html', 
                         title=f"{tag_name}标签",
                         tag=tag_name,
                         posts=posts,
                         sort=sort,
                         order=order)

# 6. 用户页路由
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/user/<username>')
def user(username):
    user = UserModel.query.filter_by(username=username).first_or_404()
    
    # 获取用户的发帖记录
    posts = PostModel.query.filter_by(author_id=user.id)\
                          .order_by(PostModel.created_at.desc())\
                          .all()
    
    # 获取用户收到的点赞
    post_likes = []
    comment_likes = []
    
    # 获取用户的评论记录
    user_comments = []
    
    if current_user.is_authenticated and current_user.username == username:
        # 查询帖子点赞
        post_likes = (db.session.query(UserPostInteraction, PostModel)
                      .join(PostModel, UserPostInteraction.post_id == PostModel.id)
                      .filter(PostModel.author_id == user.id, UserPostInteraction.liked == True)
                      .order_by(UserPostInteraction.interacted_at.desc())
                      .all())
        
        # 查询评论点赞
        comment_likes = (db.session.query(UserCommentInteraction, CommentModel)
                         .join(CommentModel, UserCommentInteraction.comment_id == CommentModel.id)
                         .filter(CommentModel.author_id == user.id, UserCommentInteraction.liked == True)
                         .order_by(UserCommentInteraction.interacted_at.desc())
                         .all())
                         
        # 获取用户的评论记录，包括帖子信息
        user_comments_query = (db.session.query(CommentModel, PostModel)
                              .join(PostModel, CommentModel.post_id == PostModel.id)
                              .filter(CommentModel.author_id == user.id)
                              .order_by(CommentModel.created_at.desc())
                              .all())
        
        # 将查询结果转换为更容易处理的格式
        user_comments = []
        for comment, post in user_comments_query:
            comment.post = post  # 将帖子信息附加到评论对象上
            user_comments.append(comment)
        
        # 已登录用户查看自己的资料
        user_info = {
            "username": user.username,
            "avatar": user.avatar,
            "join_date": user.created_at.strftime('%Y-%m-%d'),
            "bio": user.bio,
            "score": user.score  # 添加用户分数
        }
    else:
        # 未登录用户或查看他人资料
        user_info = {
            "username": user.username,
            "avatar": '/static/images/default_avatar.jpg',
            "join_date": user.created_at.strftime('%Y-%m-%d'),
            "bio": user.bio,
            "score": user.score  # 添加用户分数
        }
        
        # 获取用户的评论记录（对其他用户也显示）
        user_comments_query = (db.session.query(CommentModel, PostModel)
                              .join(PostModel, CommentModel.post_id == PostModel.id)
                              .filter(CommentModel.author_id == user.id)
                              .order_by(CommentModel.created_at.desc())
                              .all())
        
        # 将查询结果转换为更容易处理的格式
        user_comments = []
        for comment, post in user_comments_query:
            comment.post = post  # 将帖子信息附加到评论对象上
            user_comments.append(comment)
    
    return render_template('user/user.html', 
                         title=f"{username}的主页", 
                         user=user_info,
                         posts=posts,
                         post_likes=post_likes,
                         comment_likes=comment_likes,
                         user_comments=user_comments)

@app.route('/user/<username>/update', methods=['POST'])
@login_required
def update_profile(username):
    if current_user.username != username:
        flash('无权修改其他用户资料')
        return redirect(url_for('user', username=username))
    
    # 处理头像上传
    if 'avatar' in request.files:
        file = request.files['avatar']
        if file and allowed_file(file.filename):
            # 读取图片并转换为webp格式
            img = Image.open(io.BytesIO(file.read()))
            filename = f"{username}_{int(datetime.now().timestamp())}.webp"
            output_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            
            # 保存为webp格式，质量设置为80%
            img.save(output_path, 'WEBP', quality=80)
            current_user.avatar = f"/static/images/avatars/{filename}"
    
    # 更新个人简介
    current_user.bio = request.form.get('bio', '')
    
    db.session.commit()
    flash('资料更新成功')
    return redirect(url_for('user', username=username))

@app.route('/user/<username>/history')
@login_required
def user_history(username):
    if current_user.username != username:
        flash('无权查看其他用户历史')
        return redirect(url_for('user', username=username))
    
    # 从数据库获取浏览历史
    history = current_user.histories.order_by(HistoryModel.visited_at.desc()).all()
    
    return render_template('user/history.html',
                         title=f"{username}的浏览历史", 
                         history=history)

@app.route('/user/<username>/favorites')
@login_required
def user_favorites(username):
    if current_user.username != username:
        flash('无权查看其他用户收藏')
        return redirect(url_for('user', username=username))
    
    favorites = UserPostInteraction.query.filter_by(
        user_id=current_user.id,
        favorited=True
    ).order_by(UserPostInteraction.interacted_at.desc()).all()
    
    return render_template('user/favorites.html', 
                         title='我的收藏',
                         favorites=favorites)

@app.route('/user/<username>/post_likes')
@login_required
def user_post_likes(username):
    user = UserModel.query.filter_by(username=username).first_or_404()
    
    # 确保用户只能查看自己的点赞详情
    if current_user.username != username:
        flash('无权查看其他用户的点赞详情')
        return redirect(url_for('user', username=current_user.username))
    
    # 查询帖子点赞
    post_likes = (db.session.query(UserPostInteraction, PostModel)
                  .join(PostModel, UserPostInteraction.post_id == PostModel.id)
                  .filter(PostModel.author_id == user.id, UserPostInteraction.liked == True)
                  .order_by(UserPostInteraction.interacted_at.desc())
                  .all())
    
    return render_template('user/post_likes.html',
                         title=f"{username}的帖子点赞",
                         user=user,
                         post_likes=post_likes)

@app.route('/user/<username>/comment_likes')
@login_required
def user_comment_likes(username):
    user = UserModel.query.filter_by(username=username).first_or_404()
    
    # 确保用户只能查看自己的点赞详情
    if current_user.username != username:
        flash('无权查看其他用户的点赞详情')
        return redirect(url_for('user', username=current_user.username))
    
    # 查询评论点赞
    comment_likes = (db.session.query(UserCommentInteraction, CommentModel)
                     .join(CommentModel, UserCommentInteraction.comment_id == CommentModel.id)
                     .filter(CommentModel.author_id == user.id, UserCommentInteraction.liked == True)
                     .order_by(UserCommentInteraction.interacted_at.desc())
                     .all())
    
    return render_template('user/comment_likes.html',
                         title=f"{username}的评论点赞",
                         user=user,
                         comment_likes=comment_likes)

# 7. 登录路由
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        # 基本验证
        if not username:
            flash('请输入用户名')
            return render_template('user/login.html')
            
        if not password:
            flash('请输入密码')
            return render_template('user/login.html')
        
        user = UserModel.query.filter_by(username=username).first()
        if not user:
            flash('用户不存在')
            return render_template('user/login.html')
        elif not check_password_hash(user.password_hash, password):
            flash('密码错误')
            return render_template('user/login.html')
        else:
            login_user(user)
            # 重定向到用户之前访问的页面或首页
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('index'))
    return render_template('user/login.html')

# 注册路由
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        # 数据验证
        if not username:
            flash('请输入用户名')
            return render_template('user/register.html')
            
        if len(username) < 3 or len(username) > 20:
            flash('用户名长度必须在3-20个字符之间')
            return render_template('user/register.html')
            
        if not all(c.isalnum() or c == '_' for c in username):
            flash('用户名只能包含字母、数字和下划线')
            return render_template('user/register.html')
            
        if not email:
            flash('请输入邮箱地址')
            return render_template('user/register.html')
            
        if '@' not in email or '.' not in email:
            flash('请输入有效的邮箱地址')
            return render_template('user/register.html')
            
        if not password:
            flash('请输入密码')
            return render_template('user/register.html')
            
        if len(password) < 6:
            flash('密码长度至少6个字符')
            return render_template('user/register.html')
            
        if password != confirm_password:
            flash('两次输入的密码不一致')
            return render_template('user/register.html')
            
        # 检查用户名是否已存在
        existing_user = UserModel.query.filter_by(username=username).first()
        if existing_user:
            flash('用户名已存在，请选择其他用户名')
            return render_template('user/register.html')
            
        # 检查邮箱是否已被注册
        existing_email = UserModel.query.filter_by(email=email).first()
        if existing_email:
            flash('该邮箱已被注册，请使用其他邮箱')
            return render_template('user/register.html')
            
        # 创建新用户
        new_user = UserModel(
            username=username,
            email=email,
            password_hash=generate_password_hash(password)
        )
        
        try:
            db.session.add(new_user)
            db.session.commit()
            flash('注册成功，请登录')
            return redirect(url_for('login'))
        except Exception as e:
            db.session.rollback()
            flash('注册失败，请稍后重试')
            return render_template('user/register.html')
        
    return render_template('user/register.html')

# 发帖路由
@app.route('/post/new', methods=['GET', 'POST'])
@login_required
def new_post():
    if request.method == 'POST':
        title = request.form.get('title')
        content = request.form.get('content')
        tags = request.form.get('tags', '')
        category_id = request.form.get('category_id')
        
        if not title or not content:
            flash('标题和内容不能为空')
            return redirect(url_for('new_post'))
            
        if not category_id:
            flash('必须选择分类')
            return redirect(url_for('new_post'))
            
        # 处理媒体上传
        media_map = {}
        
        # 处理图片上传
        image_files = request.files.getlist('images')
        for i, file in enumerate(image_files):
            if file and file.filename and allowed_file(file.filename):
                try:
                    # 检查是否为图片
                    if file.filename.split('.')[-1].lower() in {'png', 'jpg', 'jpeg', 'gif', 'webp'}:
                        # 读取图片并转换为webp格式
                        img = Image.open(io.BytesIO(file.read()))
                        
                        # 生成文件名
                        timestamp = int(datetime.now().timestamp())
                        file_id = f"{timestamp}_{i}"
                        filename = f"post_{file_id}.webp"
                        thumb_filename = f"post_{file_id}_thumb.webp"
                        
                        # 保存原图
                        output_path = os.path.join(app.config['POST_IMAGE_FOLDER'], filename)
                        img.save(output_path, 'WEBP', quality=85)
                        image_path = f"/static/images/posts/{filename}"
                        
                        # 生成缩略图
                        img.thumbnail((300, 300))
                        thumb_output_path = os.path.join(app.config['POST_IMAGE_FOLDER'], thumb_filename)
                        img.save(thumb_output_path, 'WEBP', quality=80)
                        thumbnail_path = f"/static/images/posts/{thumb_filename}"
                        
                        # 添加到媒体映射
                        media_map[f'img{i+1}'] = {
                            'original': image_path,
                            'thumbnail': thumbnail_path
                        }
                    
                except Exception as e:
                    flash(f'图片{i+1}处理失败，请重试')
                    app.logger.error(f'Image processing error: {str(e)}')
                    return redirect(url_for('new_post'))
        
        # 处理视频上传
        video_files = request.files.getlist('videos')
        for i, file in enumerate(video_files):
            if file and file.filename and allowed_file(file.filename):
                try:
                    # 检查是否为视频
                    if file.filename.split('.')[-1].lower() in {'mp4', 'mov', 'avi', 'mkv'}:
                        # 生成文件名
                        timestamp = int(datetime.now().timestamp())
                        file_id = f"{timestamp}_{i}"
                        filename = f"post_{file_id}.{file.filename.split('.')[-1].lower()}"
                        
                        # 保存视频
                        output_path = os.path.join(app.config['POST_VIDEO_FOLDER'], filename)
                        file.save(output_path)
                        video_path = f"/static/videos/posts/{filename}"
                        
                        # 添加到媒体映射
                        media_map[f'video{i+1}'] = {
                            'original': video_path
                        }
                    
                except Exception as e:
                    flash(f'视频{i+1}处理失败，请重试')
                    app.logger.error(f'Video processing error: {str(e)}')
                    return redirect(url_for('new_post'))
        
        # 处理内容中的媒体标记
        if media_map:
            for marker, media_info in media_map.items():
                if marker.startswith('img'):
                    # 替换图片标记为img标签
                    content = content.replace(
                        f'[{marker}]', 
                        f'<img src="{media_info["original"]}" class="img-fluid" alt="图片">'
                    )
                elif marker.startswith('video'):
                    # 替换视频标记为video标签
                    content = content.replace(
                        f'[{marker}]',
                        f'<video controls class="img-fluid"><source src="{media_info["original"]}"></video>'
                    )
        
        # 初始化媒体路径变量
        image_path = None
        thumbnail_path = None
        video_path = None
        
        post = PostModel(
            title=title,
            content=content,
            author_id=current_user.id,
            tags=tags,
            image_path=image_path,
            thumbnail_path=thumbnail_path,
            video_path=video_path,
            category_id=category_id
        )
        db.session.add(post)
        current_user.post_count += 1
        db.session.commit()
        
        flash('帖子发布成功')
        return redirect(url_for('index'))
    
    # 获取URL中的标签参数
    tag = request.args.get('tag', '')
    # 获取所有主分类
    categories = CategoryModel.query.filter_by(is_main=True).all()
    return render_template('post/new.html', tag=tag, categories=categories)

# 9. 登出路由
@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

# 10. 搜索路由
@app.route('/search')
def search():
    query = request.args.get('q', '')
    if not query:
        return redirect(url_for('index'))
    
    # 使用or_组合多个搜索条件
    from sqlalchemy import or_
    
    # 搜索帖子标题、内容、标签和作者用户名
    posts = PostModel.query.join(UserModel).filter(
        or_(
            PostModel.title.ilike(f'%{query}%'),
            PostModel.content.ilike(f'%{query}%'),
            PostModel.tags.ilike(f'%{query}%'),
            UserModel.username.ilike(f'%{query}%')
        )
    ).order_by(PostModel.created_at.desc()).all()
    
    return render_template('search.html', 
                         title=f"搜索: {query}",
                         posts=posts,
                         query=query)

# 图片上传路由
@app.route('/upload_image', methods=['POST'])
def upload_image():
    if 'image' not in request.files:
        return jsonify({'success': False, 'message': '未选择文件'}), 400
    
    file = request.files['image']
    if file.filename == '':
        return jsonify({'success': False, 'message': '未选择文件'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'success': False, 'message': '不支持的文件类型'}), 400
    
    # 生成唯一文件名
    timestamp = int(datetime.now().timestamp())
    ext = file.filename.rsplit('.', 1)[1].lower()
    filename = f"post_{timestamp}.{ext}"
    filepath = os.path.join(app.config['POST_IMAGE_FOLDER'], filename)
    
    try:
        file.save(filepath)
        return jsonify({
            'success': True,
            'url': f"/static/images/posts/{filename}"
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

# 视频上传路由
@app.route('/upload_video', methods=['POST'])
def upload_video():
    if 'video' not in request.files:
        return jsonify({'success': False, 'message': '未选择文件'}), 400
    
    file = request.files['video']
    if file.filename == '':
        return jsonify({'success': False, 'message': '未选择文件'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'success': False, 'message': '不支持的文件类型'}), 400
    
    # 生成唯一文件名
    timestamp = int(datetime.now().timestamp())
    ext = file.filename.rsplit('.', 1)[1].lower()
    filename = f"post_{timestamp}.{ext}"
    filepath = os.path.join(app.config['POST_VIDEO_FOLDER'], filename)
    
    try:
        file.save(filepath)
        return jsonify({
            'success': True,
            'url': f"/static/videos/posts/{filename}"
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

# 浏览历史路由
@app.route('/post/<int:post_id>/likes')
def post_likes(post_id):
    post = PostModel.query.get_or_404(post_id)
    
    # 只有帖子作者或管理员可以查看点赞详情
    if not current_user.is_authenticated or (current_user.id != post.author_id and not current_user.is_admin):
        flash('无权查看此内容')
        return redirect(url_for('post', post_id=post_id))
    
    # 获取所有点赞记录
    likes = (db.session.query(UserPostInteraction, UserModel)
             .join(UserModel, UserPostInteraction.user_id == UserModel.id)
             .filter(UserPostInteraction.post_id == post_id, UserPostInteraction.liked == True)
             .order_by(UserPostInteraction.interacted_at.desc())
             .all())
    
    return render_template('post/likes.html', 
                         title=f"{post.title}的点赞记录",
                         post=post,
                         likes=likes)

@app.route('/api/clear_history', methods=['POST'])
@login_required
def clear_history():
    try:
        # 删除当前用户的所有浏览历史
        HistoryModel.query.filter_by(user_id=current_user.id).delete()
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

# 记录浏览历史的中间件
@app.before_request
def before_request():
    if current_user.is_authenticated:
        current_user.last_seen = utc_time()
        db.session.commit()
        
        # 只记录帖子浏览历史
        if request.method == 'GET' and request.endpoint == 'post':
            post_id = request.view_args.get('post_id')
            post = PostModel.query.get(post_id)
            if post:
                # 使用url_for生成完整的URL
                post_url = url_for('post', post_id=post_id)
                
                # 检查是否已存在该帖子的浏览记录
                existing_history = HistoryModel.query.filter_by(
                    user_id=current_user.id,
                    url=post_url
                ).first()
                
                if existing_history:
                    # 如果已存在，则更新访问时间
                    existing_history.visited_at = utc_time()
                    existing_history.title = post.title
                else:
                    # 如果不存在，则创建新记录
                    history = HistoryModel(
                        user_id=current_user.id,
                        url=post_url,
                        title=post.title
                    )
                    db.session.add(history)
                
                db.session.commit()

def get_hot_tags(limit=10):
    """获取热门标签（近一个月内热度最高的帖子中的标签，按出现频率排序）"""
    one_month_ago = utc_time() - timedelta(days=30)
    
    # 获取近一个月内热度最高的帖子（按浏览量+点赞数*10+收藏数*5计算热度）
    hot_posts = PostModel.query.filter(PostModel.created_at >= one_month_ago).all()
    
    # 统计标签频率
    tag_counts = {}
    for post in hot_posts:
        if post.tags:
            tags = [tag.strip() for tag in post.tags.split(',')]
            for tag in tags:
                if tag:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
    
    # 按频率排序并取前N个
    sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    return [tag[0] for tag in sorted_tags]

def get_hot_tags_with_count(limit=10):
    """获取热门标签及计数（近一个月内热度最高的帖子中的标签，按出现频率排序）"""
    one_month_ago = utc_time() - timedelta(days=30)
    
    # 获取近一个月内热度最高的帖子（按浏览量+点赞数*10+收藏数*5计算热度）
    hot_posts = PostModel.query.filter(PostModel.created_at >= one_month_ago).all()
    
    # 统计标签频率
    tag_counts = {}
    for post in hot_posts:
        if post.tags:
            tags = [tag.strip() for tag in post.tags.split(',')]
            for tag in tags:
                if tag:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
    
    # 按频率排序并取前N个
    sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    return sorted_tags

# 上下文处理器 - 自动向模板注入current_user、format_time、latest_posts和hot_tags
@app.context_processor
def inject_user():
    latest_posts = PostModel.query.order_by(PostModel.created_at.desc()).limit(5).all()
    hot_tags_with_count = get_hot_tags_with_count()
    return dict(
        current_user=current_user, 
        naturaltime=format_time,
        latest_posts=latest_posts,
        hot_tags_with_count=hot_tags_with_count,
        hot_tags=[tag[0] for tag in hot_tags_with_count]  # 保持向后兼容
    )

# 初始化分类数据
def init_categories():
    categories = [
        {'name': '物理学', 'is_main': True, 'children': [
            '经典力学', '量子物理', '热力学', '电磁学', '粒子物理'
        ]},
        {'name': '化学', 'is_main': True, 'children': [
            '无机化学', '有机化学', '物理化学', '分析化学', '生物化学'
        ]},
        {'name': '生物学', 'is_main': True, 'children': [
            '动物学', '植物学', '生态学', '进化生物学', '分类学',
            '细胞生物学', '分子生物学', '微生物学', '遗传学', '生物化学'
        ]},
        {'name': '地球科学', 'is_main': True, 'children': [
            '地质学', '气象学', '海洋学', '环境科学', '地理学'
        ]},
        {'name': '天文学', 'is_main': True, 'children': [
            '宇宙学', '天体物理学', '星系天文学', '行星科学', '宇宙探索'
        ]},
        {'name': '数学', 'is_main': True, 'children': [
            '代数', '几何', '微积分', '数论', '统计学', '拓扑学'
        ]},
        {'name': '军事', 'is_main': True, 'children': [
            '军事理论', '军事历史', '军事技术', '战略研究', '国防科技'
        ]},
        {'name': '政治', 'is_main': True, 'children': [
            '政治理论', '国际关系', '政治经济学', '公共政策', '比较政治'
        ]},
        {'name': '人文', 'is_main': True, 'children': [
            '哲学', '历史', '文学', '艺术', '宗教'
        ]},
        {'name': '计算机科学', 'is_main': True, 'children': [
            '编程', '人工智能', '区块链', '云计算', '大数据'
        ]},
        {'name': '生活', 'is_main': True, 'children': [
            '健康', '美食', '旅行', '运动', '心理', '音乐'
        ]}
    ]
    
    for cat_data in categories:
        # 检查大类是否已存在，如果不存在则创建
        main_cat = CategoryModel.query.filter_by(name=cat_data['name'], is_main=True).first()
        if not main_cat:
            main_cat = CategoryModel(
                name=cat_data['name'],
                is_main=True
            )
            db.session.add(main_cat)
            db.session.flush()  # 使用flush而不是commit，确保可以获取到id
        
        # 添加或更新子分类
        for child in cat_data['children']:
            # 处理嵌套的子分类结构
            if isinstance(child, dict):
                # 处理有子分类的分类
                sub_cat = CategoryModel.query.filter_by(name=child['name'], parent_id=main_cat.id).first()
                if not sub_cat:
                    sub_cat = CategoryModel(
                        name=child['name'],
                        parent_id=main_cat.id,
                        is_main=False
                    )
                    db.session.add(sub_cat)
                    db.session.flush()  # 使用flush而不是commit
                
                # 递归处理子分类
                for sub_child in child.get('children', []):
                    child_cat = CategoryModel.query.filter_by(name=sub_child, parent_id=sub_cat.id).first()
                    if not child_cat:
                        child_cat = CategoryModel(
                            name=sub_child,
                            parent_id=sub_cat.id,
                            is_main=False
                        )
                        db.session.add(child_cat)
            else:
                # 处理简单的子分类字符串
                child_cat = CategoryModel.query.filter_by(name=child, parent_id=main_cat.id).first()
                if not child_cat:
                    child_cat = CategoryModel(
                        name=child,
                        parent_id=main_cat.id,
                        is_main=False
                    )
                    db.session.add(child_cat)
    
    # 提交所有更改
    db.session.commit()

def init_user_scores():
    """初始化所有用户的分数"""
    users = UserModel.query.all()
    for user in users:
        user.update_score()
    db.session.commit()

def init_database():
    """初始化数据库"""
    # 创建所有表
    db.create_all()
    
    # 初始化分类数据
    init_categories()
    
    # 初始化所有用户分数
    init_user_scores()

@app.route('/help')
def help():
    return render_template('help.html')

@app.route('/help/guide')
def help_guide():
    return render_template('help/guide.html')

@app.route('/help/faq')
def help_faq():
    return render_template('help/faq.html')

@app.route('/help/feedback', methods=['GET', 'POST'])
def help_feedback():
    if request.method == 'POST':
        # 获取表单数据
        subject = request.form.get('subject')
        content = request.form.get('content')
        contact = request.form.get('contact')
        
        # 这里应该处理反馈数据，比如保存到数据库或发送邮件
        # 目前我们只是简单地显示一个成功消息
        flash('感谢您的反馈！我们会尽快处理您的建议。', 'success')
        return redirect(url_for('help_feedback'))
    
    return render_template('help/feedback.html')

# 初始化数据库
with app.app_context():
    init_database()
    # 临时检查数据库结构
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    columns = inspector.get_columns('user_model')
    with open('temp_db_check.txt', 'w') as f:
        f.write(','.join([c['name'] for c in columns]))

# 临时路由用于检查数据库结构
@app.route('/_check_db')
def check_db():
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    columns = inspector.get_columns('user_model')
    return {'columns': [c['name'] for c in columns]}

# 启动应用（仅开发环境用）
if __name__ == '__main__':
    with app.app_context():
        init_database()
    app.run(host='0.0.0.0', port=9000)  # debug=True：代码修改后自动重启，报错时显示调试页面
