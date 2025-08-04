#!/usr/bin/env python3
"""
Fraud Detection Engine - Main Application
Real-time ML-powered fraud detection system

Author: Your Name
Version: 2.1.0
"""

import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
import json
import numpy as np
import pandas as pd
from dataclasses import dataclass, asdict
from enum import Enum
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import redis
import joblib
from sklearn.ensemble import RandomForestClassifier, IsolationForest
from sklearn.preprocessing import StandardScaler, LabelEncoder
import xgboost as xgb
import kafka
from kafka import KafkaConsumer, KafkaProducer
import psycopg2
from contextlib import asynccontextmanager
import warnings
warnings.filterwarnings('ignore')

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('fraud_detection.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class RiskLevel(Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"  
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

class ActionType(Enum):
    APPROVE = "APPROVE"
    REVIEW = "REVIEW"
    DECLINE = "DECLINE"
    BLOCK_CARD = "BLOCK_CARD"

@dataclass
class Transaction:
    transaction_id: str
    amount: float
    merchant_category: str
    location: str
    timestamp: datetime
    card_number: str
    account_id: str
    merchant_name: str
    currency: str = "USD"
    channel: str = "online"

@dataclass
class FraudPrediction:
    transaction_id: str
    fraud_score: float
    risk_level: RiskLevel
    action: ActionType
    confidence: float
    features_importance: Dict[str, float]
    model_version: str
    processing_time_ms: float
    explanation: List[str]

class FeatureEngineer:
    """Advanced feature engineering for fraud detection"""
    
    def __init__(self, redis_client):
        self.redis_client = redis_client
        
    def extract_features(self, transaction: Transaction) -> Dict[str, Any]:
        """Extract comprehensive features from transaction"""
        features = {}
        
        # Basic transaction features
        features['amount'] = transaction.amount
        features['hour'] = transaction.timestamp.hour
        features['day_of_week'] = transaction.timestamp.weekday()
        features['is_weekend'] = transaction.timestamp.weekday() >= 5
        
        # Account-based features
        account_key = f"account:{transaction.account_id}"
        account_data = self.redis_client.hgetall(account_key)
        
        if account_data:
            features['account_age_days'] = int(account_data.get(b'age_days', 0))
            features['avg_transaction_amount'] = float(account_data.get(b'avg_amount', 0))
            features['transaction_count_30d'] = int(account_data.get(b'count_30d', 0))
        else:
            features['account_age_days'] = 0
            features['avg_transaction_amount'] = 0
            features['transaction_count_30d'] = 0
            
        # Velocity features (transactions in last hour/day)
        velocity_key = f"velocity:{transaction.account_id}"
        velocity_data = self.redis_client.zrange(velocity_key, 0, -1, withscores=True)
        
        current_time = transaction.timestamp.timestamp()
        hour_ago = current_time - 3600
        day_ago = current_time - 86400
        
        features['velocity_1h'] = len([t for t, ts in velocity_data if ts >= hour_ago])
        features['velocity_24h'] = len([t for t, ts in velocity_data if ts >= day_ago])
        
        # Amount-based features
        if features['avg_transaction_amount'] > 0:
            features['amount_z_score'] = (transaction.amount - features['avg_transaction_amount']) / max(features['avg_transaction_amount'] * 0.5, 1)
        else:
            features['amount_z_score'] = 0
            
        features['amount_log'] = np.log1p(transaction.amount)
        features['is_round_amount'] = int(transaction.amount % 1 == 0)
        
        # Categorical features (will be encoded)
        features['merchant_category'] = transaction.merchant_category
        features['location'] = transaction.location
        features['channel'] = transaction.channel
        
        # Geographic risk (simplified)
        high_risk_locations = ['Unknown', 'Nigeria', 'Russia', 'China']
        features['geo_risk'] = int(any(loc in transaction.location for loc in high_risk_locations))
        
        # Merchant risk
        high_risk_categories = ['gambling', 'adult', 'cryptocurrency', 'money_transfer']
        features['merchant_risk'] = int(transaction.merchant_category.lower() in high_risk_categories)
        
        return features

class MLModelEnsemble:
    """Ensemble of ML models for fraud detection"""
    
    def __init__(self):
        self.models = {}
        self.scaler = StandardScaler()
        self.label_encoders = {}
        self.feature_names = []
        self.model_version = "2.1.0"
        self.is_trained = False
        
    def train(self, X: pd.DataFrame, y: pd.Series):
        """Train the ensemble of models"""
        logger.info("Training fraud detection models...")
        
        # Prepare features
        X_processed = self._preprocess_features(X, fit=True)
        
        # Train Random Forest
        self.models['random_forest'] = RandomForestClassifier(
            n_estimators=100,
            max_depth=15,
            random_state=42,
            n_jobs=-1
        )
        self.models['random_forest'].fit(X_processed, y)
        
        # Train XGBoost
        self.models['xgboost'] = xgb.XGBClassifier(
            n_estimators=100,
            max_depth=8,
            learning_rate=0.1,
            random_state=42,
            n_jobs=-1
        )
        self.models['xgboost'].fit(X_processed, y)
        
        # Train Isolation Forest for anomaly detection
        self.models['isolation_forest'] = IsolationForest(
            n_estimators=100,
            contamination=0.1,
            random_state=42,
            n_jobs=-1
        )
        self.models['isolation_forest'].fit(X_processed)
        
        self.feature_names = X.columns.tolist()
        self.is_trained = True
        logger.info("Model training completed successfully")
        
    def _preprocess_features(self, X: pd.DataFrame, fit: bool = False) -> np.ndarray:
        """Preprocess features for ML models"""
        X_copy = X.copy()
        
        # Handle categorical features
        categorical_features = ['merchant_category', 'location', 'channel']
        
        for feature in categorical_features:
            if feature in X_copy.columns:
                if fit:
                    if feature not in self.label_encoders:
                        self.label_encoders[feature] = LabelEncoder()
                    X_copy[feature] = self.label_encoders[feature].fit_transform(X_copy[feature].astype(str))
                else:
                    if feature in self.label_encoders:
                        # Handle unknown categories
                        unique_values = self.label_encoders[feature].classes_
                        X_copy[feature] = X_copy[feature].astype(str).apply(
                            lambda x: x if x in unique_values else 'unknown'
                        )
                        try:
                            X_copy[feature] = self.label_encoders[feature].transform(X_copy[feature])
                        except ValueError:
                            # If still unknown, use 0
                            X_copy[feature] = 0
        
        # Scale numerical features
        if fit:
            X_scaled = self.scaler.fit_transform(X_copy)
        else:
            X_scaled = self.scaler.transform(X_copy)
            
        return X_scaled
        
    def predict(self, X: pd.DataFrame) -> Dict[str, Any]:
        """Make fraud prediction using ensemble"""
        if not self.is_trained:
            raise ValueError("Models not trained yet")
            
        X_processed = self._preprocess_features(X, fit=False)
        
        # Get predictions from each model
        rf_proba = self.models['random_forest'].predict_proba(X_processed)[0][1]
        xgb_proba = self.models['xgboost'].predict_proba(X_processed)[0][1]
        iso_score = self.models['isolation_forest'].decision_function(X_processed)[0]
        
        # Normalize isolation forest score to 0-1
        iso_score_norm = max(0, min(1, (iso_score + 0.5) / 1.0))
        
        # Ensemble prediction (weighted average)
        fraud_score = (rf_proba * 0.4 + xgb_proba * 0.4 + (1 - iso_score_norm) * 0.2)
        
        # Feature importance (from Random Forest)
        feature_importance = dict(zip(
            self.feature_names,
            self.models['random_forest'].feature_importances_
        ))
        
        return {
            'fraud_score': fraud_score,
            'model_scores': {
                'random_forest': rf_proba,
                'xgboost': xgb_proba,
                'isolation_forest': 1 - iso_score_norm
            },
            'feature_importance': feature_importance
        }

class SplunkAlerter:
    """Splunk integration for automated alerting"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.alert_thresholds = {
            'high_fraud_rate': 0.8,
            'volume_spike': 1.5,
            'system_error': True
        }
        
    async def send_alert(self, alert_type: str, data: Dict[str, Any]):
        """Send alert to Splunk"""
        try:
            # In a real implementation, this would use Splunk SDK
            alert_data = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'alert_type': alert_type,
                'severity': self._get_severity(alert_type),
                'data': data,
                'source': 'fraud_detection_engine'
            }
            
            logger.warning(f"SPLUNK ALERT [{alert_type}]: {json.dumps(alert_data, indent=2)}")
            
            # Simulate Splunk HTTP Event Collector
            await asyncio.sleep(0.01)  # Simulate network call
            
        except Exception as e:
            logger.error(f"Failed to send Splunk alert: {e}")
            
    def _get_severity(self, alert_type: str) -> str:
        severity_map = {
            'high_fraud_transaction': 'HIGH',
            'system_error': 'CRITICAL',
            'volume_spike': 'MEDIUM',
            'model_drift': 'LOW'
        }
        return severity_map.get(alert_type, 'MEDIUM')

class FraudDetector:
    """Main fraud detection engine"""
    
    def __init__(self):
        # Initialize components
        self.redis_client = redis.Redis(host='localhost', port=6379, decode_responses=False)
        self.feature_engineer = FeatureEngineer(self.redis_client)
        self.ml_model = MLModelEnsemble()
        self.splunk_alerter = SplunkAlerter({})
        
        # Performance metrics
        self.metrics = {
            'total_transactions': 0,
            'fraud_detected': 0,
            'processing_times': [],
            'start_time': time.time()
        }
        
        # Load pre-trained models if available
        self._load_models()
        
    def _load_models(self):
        """Load pre-trained models or train on synthetic data"""
        try:
            # In production, load from file
            # self.ml_model = joblib.load('models/fraud_model.pkl')
            # For demo, train on synthetic data
            self._train_on_synthetic_data()
        except Exception as e:
            logger.warning(f"Could not load models: {e}. Training on synthetic data.")
            self._train_on_synthetic_data()
            
    def _train_on_synthetic_data(self):
        """Train models on synthetic transaction data"""
        logger.info("Generating synthetic training data...")
        
        # Generate synthetic features
        np.random.seed(42)
        n_samples = 10000
        
        # Normal transactions (90%)
        normal_data = {
            'amount': np.random.lognormal(3, 1, int(n_samples * 0.9)),
            'hour': np.random.randint(0, 24, int(n_samples * 0.9)),
            'day_of_week': np.random.randint(0, 7, int(n_samples * 0.9)),
            'is_weekend': np.random.choice([0, 1], int(n_samples * 0.9), p=[0.7, 0.3]),
            'account_age_days': np.random.randint(30, 3650, int(n_samples * 0.9)),
            'velocity_1h': np.random.poisson(1, int(n_samples * 0.9)),
            'velocity_24h': np.random.poisson(8, int(n_samples * 0.9)),
            'amount_z_score': np.random.normal(0, 1, int(n_samples * 0.9)),
            'geo_risk': np.random.choice([0, 1], int(n_samples * 0.9), p=[0.95, 0.05]),
            'merchant_risk': np.random.choice([0, 1], int(n_samples * 0.9), p=[0.9, 0.1]),
            'merchant_category': np.random.choice(['retail', 'grocery', 'gas', 'restaurant'], int(n_samples * 0.9)),
            'location': np.random.choice(['US', 'Canada', 'UK', 'Germany'], int(n_samples * 0.9)),
            'channel': np.random.choice(['online', 'atm', 'pos'], int(n_samples * 0.9))
        }
        
        # Fraudulent transactions (10%)
        fraud_data = {
            'amount': np.random.lognormal(4, 1.5, int(n_samples * 0.1)),  # Higher amounts
            'hour': np.random.choice([2, 3, 4, 22, 23], int(n_samples * 0.1)),  # Unusual hours
            'day_of_week': np.random.randint(0, 7, int(n_samples * 0.1)),
            'is_weekend': np.random.choice([0, 1], int(n_samples * 0.1)),
            'account_age_days': np.random.randint(1, 90, int(n_samples * 0.1)),  # New accounts
            'velocity_1h': np.random.poisson(5, int(n_samples * 0.1)),  # High velocity
            'velocity_24h': np.random.poisson(20, int(n_samples * 0.1)),
            'amount_z_score': np.random.normal(3, 2, int(n_samples * 0.1)),  # Unusual amounts
            'geo_risk': np.random.choice([0, 1], int(n_samples * 0.1), p=[0.3, 0.7]),  # High geo risk
            'merchant_risk': np.random.choice([0, 1], int(n_samples * 0.1), p=[0.4, 0.6]),  # High merchant risk
            'merchant_category': np.random.choice(['gambling', 'cryptocurrency', 'adult'], int(n_samples * 0.1)),
            'location': np.random.choice(['Unknown', 'Nigeria', 'Russia'], int(n_samples * 0.1)),
            'channel': np.random.choice(['online', 'atm', 'pos'], int(n_samples * 0.1))
        }
        
        # Combine data
        X_data = {}
        for key in normal_data.keys():
            X_data[key] = np.concatenate([normal_data[key], fraud_data[key]])
            
        X = pd.DataFrame(X_data)
        y = pd.Series([0] * int(n_samples * 0.9) + [1] * int(n_samples * 0.1))
        
        # Train the model
        self.ml_model.train(X, y)
        
    async def predict(self, transaction: Transaction) -> FraudPrediction:
        """Predict fraud probability for a transaction"""
        start_time = time.time()
        
        try:
            # Update metrics
            self.metrics['total_transactions'] += 1
            
            # Extract features
            features = self.feature_engineer.extract_features(transaction)
            X = pd.DataFrame([features])
            
            # Get ML prediction
            ml_result = self.ml_model.predict(X)
            fraud_score = ml_result['fraud_score']
            
            # Determine risk level and action
            risk_level, action = self._determine_risk_and_action(fraud_score)
            
            # Calculate confidence
            confidence = self._calculate_confidence(fraud_score, ml_result['model_scores'])
            
            # Generate explanation
            explanation = self._generate_explanation(features, ml_result['feature_importance'], fraud_score)
            
            # Processing time
            processing_time = (time.time() - start_time) * 1000
            self.metrics['processing_times'].append(processing_time)
            
            # Create prediction result
            prediction = FraudPrediction(
                transaction_id=transaction.transaction_id,
                fraud_score=fraud_
