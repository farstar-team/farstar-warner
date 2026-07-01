from sqlalchemy import Column, String, BigInteger, Integer, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    
    telegram_id = Column(BigInteger, primary_key=True)
    username = Column(String, nullable=True)
    subscription_expiry = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default="active")  # active, banned
    plan_tier = Column(String, default="free")  # free, premium, vip
    
    targets = relationship("TargetPage", back_populates="user")

class TargetPage(Base):
    __tablename__ = 'target_pages'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    instagram_username = Column(String, nullable=False)
    last_known_status = Column(String, default="unknown")  # active, deactivated
    instagram_id = Column(String, nullable=True)
    
    # متادیتای آماری پیج
    follower_count = Column(Integer, default=0)
    following_count = Column(Integer, default=0)
    post_count = Column(Integer, default=0)
    full_name = Column(String, nullable=True)
    
    user_id = Column(BigInteger, ForeignKey('users.telegram_id'))
    user = relationship("User", back_populates="targets")
