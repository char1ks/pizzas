"""
Pizza Order System - Notification Service
Event-Driven Saga Architecture

Handles user notifications through multiple channels (email, SMS, push, webhook)
"""

import os
import sys
import json
import threading
import time
from typing import Dict, List, Any, Optional
from flask import request, jsonify
from flask_cors import CORS
from datetime import datetime, timezone
from enum import Enum

# Add shared module to path
sys.path.insert(0, '/app/shared')

from base_service import BaseService, generate_id, validate_required_fields, ValidationError, retry_with_backoff


class NotificationType(Enum):
    """Notification type enumeration"""
    EMAIL = "EMAIL"
    SMS = "SMS"
    PUSH = "PUSH"
    WEBHOOK = "WEBHOOK"


class NotificationStatus(Enum):
    """Notification status enumeration"""
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    DELIVERED = "DELIVERED"


class NotificationService(BaseService):
    """Notification Service for sending user notifications"""
    
    def __init__(self):
        super().__init__('notification-service')
        
        # Enable CORS for web UI
        CORS(self.app, origins=['*'])
        
        # Configuration
        self.email_enabled = os.getenv('EMAIL_ENABLED', 'true').lower() == 'true'
        self.sms_enabled = os.getenv('SMS_ENABLED', 'true').lower() == 'true'
        self.push_enabled = os.getenv('PUSH_ENABLED', 'true').lower() == 'true'
        self.webhook_enabled = os.getenv('WEBHOOK_ENABLED', 'true').lower() == 'true'
        
        # Rate limiting
        self.max_notifications_per_minute = int(os.getenv('MAX_NOTIFICATIONS_PER_MINUTE', '100'))
        
        # Setup routes
        self.setup_routes()
        
        # Initialize database
        self.init_database_with_schema_creation('notifications', 'SELECT 1')
        
        # Create default templates
        self.create_default_templates()
        
        # Start event consumer in background thread
        self.start_event_consumer()
        
        self.logger.info(
            "Notification Service initialized",
            email_enabled=self.email_enabled,
            sms_enabled=self.sms_enabled,
            push_enabled=self.push_enabled,
            webhook_enabled=self.webhook_enabled
        )

    def create_default_templates(self):
        """Create default notification templates if they don't exist."""
        templates = [
            {'type': 'OrderCreated', 'title_template': 'Pizza Order Confirmed', 'message_template': 'Your pizza order #{orderId} has been confirmed. Total: ${totalAmount/100:.2f}. We\'ll notify you when payment is processed.'},
            {'type': 'OrderPaid', 'title_template': 'Payment Successful', 'message_template': 'Payment of ${amount/100:.2f} for order #{order_id} was successful. Your pizza is being prepared!'},
            {'type': 'PaymentFailed', 'title_template': 'Payment Failed', 'message_template': 'Payment for order #{order_id} failed: {failure_reason}. Please try again or contact support.'}
        ]
        
        try:
            with self.db.get_cursor() as cursor:
                for t in templates:
                    cursor.execute(
                        """
                        INSERT INTO notifications.notification_templates (type, title_template, message_template)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (type) DO NOTHING
                        """,
                        (t['type'], t['title_template'], t['message_template'])
                    )
            self.logger.info("Default notification templates checked/created.")
        except Exception as e:
            self.logger.error("Failed to create default notification templates", error=str(e))

    def setup_routes(self):
        """Setup API routes for notification service"""
        
        @self.app.route('/api/v1/notifications', methods=['POST'])
        def send_notification():
            """Send notification through specified channels"""
            try:
                data = request.get_json()
                
                # Validate required fields
                required_fields = ['userId', 'message', 'channels']
                missing_fields = validate_required_fields(data, required_fields)
                
                if missing_fields:
                    raise ValidationError(f"Missing required fields: {', '.join(missing_fields)}")
                
                user_id = data['userId']
                subject = data.get('subject', 'Pizza Order Notification')
                message = data['message']
                channels = data.get('channels', ['EMAIL']) # Default to email
                order_id = data.get('orderId')
                priority = data.get('priority', 'normal')
                
                # Validate channels
                valid_channels = [t.value for t in NotificationType]
                invalid_channels = [c for c in channels if c not in valid_channels]
                if invalid_channels:
                    raise ValidationError(f"Invalid channels: {', '.join(invalid_channels)}")
                
                # Generate notification ID
                notification_id = generate_id('notif_')
                
                # Create notification record
                self.create_notification_record(
                    notification_id=notification_id,
                    user_id=user_id,
                    order_id=order_id,
                    subject=subject,
                    message=message,
                    channels=channels,
                    priority=priority
                )
                
                # Send notifications asynchronously
                threading.Thread(
                    target=self.send_notification_async,
                    args=(notification_id,),
                    daemon=True
                ).start()
                
                self.logger.info(
                    "Notification queued for sending",
                    notification_id=notification_id,
                    user_id=user_id,
                    channels=channels,
                    priority=priority
                )
                
                self.metrics.record_business_event('notification_queued', 'success')
                
                return jsonify({
                    'success': True,
                    'notificationId': notification_id,
                    'status': 'PENDING',
                    'channels': channels,
                    'timestamp': self.get_timestamp()
                }), 202  # Accepted - processing asynchronously
                
            except ValidationError as e:
                self.logger.warning("Notification validation failed", error=str(e))
                return jsonify({
                    'success': False,
                    'error': 'Validation error',
                    'message': str(e)
                }), 400
                
            except Exception as e:
                self.logger.error("Failed to queue notification", error=str(e))
                self.metrics.record_business_event('notification_queued', 'failed')
                
                return jsonify({
                    'success': False,
                    'error': 'Failed to queue notification',
                    'message': str(e)
                }), 500

    def create_notification_record(self, notification_id: str, user_id: str, order_id: str,
                                  subject: str, message: str, channels: List[str], priority: str,
                                  template_type: str = None) -> Dict:
        """Create notification record in database"""
        try:
            with self.db.get_cursor() as cursor:
                cursor.execute("""
                    INSERT INTO notifications.notifications 
                    (id, user_id, order_id, subject, message, channels, priority, template_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (notification_id, user_id, order_id, subject, message, json.dumps(channels), priority, template_type))
                
            return {
                'id': notification_id,
                'user_id': user_id,
                'order_id': order_id,
                'subject': subject,
                'message': message,
                'channels': channels,
                'priority': priority,
                'status': 'PENDING'
            }
        except Exception as e:
            self.logger.error("Failed to create notification record", notification_id=notification_id, error=str(e))
            raise

    def send_notification_async(self, notification_id: str):
        """Send notification asynchronously"""
        try:
            notification = self.get_notification_by_id(notification_id)
            if not notification:
                self.logger.error("Notification not found", notification_id=notification_id)
                return
            
            # Get user contact info
            contact_info = self.get_user_contact_info(notification['user_id'])
            
            success = True
            for channel in notification['channels']:
                try:
                    channel_success = self.send_through_channel(notification, channel, contact_info)
                    if not channel_success:
                        success = False
                except Exception as e:
                    self.logger.error("Channel send failed", channel=channel, error=str(e))
                    success = False
            
            # Update notification status
            final_status = 'SENT' if success else 'FAILED'
            self.update_notification_status(notification_id, final_status)
            
        except Exception as e:
            self.logger.error("Async notification send failed", notification_id=notification_id, error=str(e))
            self.update_notification_status(notification_id, 'FAILED')

    def send_through_channel(self, notification: Dict, channel: str, contact_info: Dict) -> bool:
        """Send notification through specific channel"""
        try:
            if channel == 'EMAIL' and self.email_enabled:
                return self.send_email_notification(notification, contact_info)
            elif channel == 'SMS' and self.sms_enabled:
                return self.send_sms_notification(notification, contact_info)
            elif channel == 'PUSH' and self.push_enabled:
                return self.send_push_notification(notification, contact_info)
            elif channel == 'WEBHOOK' and self.webhook_enabled:
                return self.send_webhook_notification(notification, contact_info)
            else:
                self.logger.warning("Channel disabled or unknown", channel=channel)
                return False
        except Exception as e:
            self.logger.error("Failed to send through channel", channel=channel, error=str(e))
            return False

    def send_email_notification(self, notification: Dict, contact_info: Dict) -> bool:
        """Send email notification (mock implementation)"""
        try:
            self.logger.info(
                "📧 Sending email notification",
                notification_id=notification['id'],
                to=contact_info.get('email', 'unknown@example.com'),
                subject=notification['subject']
            )
            time.sleep(0.1)  # Simulate email sending delay
            return True
        except Exception as e:
            self.logger.error("Email send failed", error=str(e))
            return False

    def send_sms_notification(self, notification: Dict, contact_info: Dict) -> bool:
        """Send SMS notification (mock implementation)"""
        try:
            self.logger.info(
                "📱 Sending SMS notification",
                notification_id=notification['id'],
                to=contact_info.get('phone', '+1234567890'),
                message=notification['message'][:160]  # SMS length limit
            )
            time.sleep(0.1)  # Simulate SMS sending delay
            return True
        except Exception as e:
            self.logger.error("SMS send failed", error=str(e))
            return False

    def send_push_notification(self, notification: Dict, contact_info: Dict) -> bool:
        """Send push notification (mock implementation)"""
        try:
            self.logger.info(
                "🔔 Sending push notification",
                notification_id=notification['id'],
                to=contact_info.get('device_token', 'unknown_device'),
                title=notification['subject']
            )
            time.sleep(0.1)  # Simulate push sending delay
            return True
        except Exception as e:
            self.logger.error("Push send failed", error=str(e))
            return False

    def send_webhook_notification(self, notification: Dict, contact_info: Dict) -> bool:
        """Send webhook notification (mock implementation)"""
        try:
            self.logger.info(
                "🪝 Sending webhook notification",
                notification_id=notification['id'],
                webhook_url=contact_info.get('webhook_url', 'https://example.com/webhook')
            )
            time.sleep(0.1)  # Simulate webhook delay
            return True
        except Exception as e:
            self.logger.error("Webhook send failed", error=str(e))
            return False

    def get_user_contact_info(self, user_id: str) -> Dict:
        """Get user contact information (mock implementation)"""
        return {
            'email': f'{user_id}@example.com',
            'phone': '+1234567890',
            'device_token': f'device_token_{user_id}',
            'webhook_url': 'https://example.com/webhook'
        }

    def get_notification_by_id(self, notification_id: str) -> Optional[Dict]:
        """Get notification by ID from database"""
        try:
            notification = self.db.execute_query(
                "SELECT * FROM notifications.notifications WHERE id = %s",
                (notification_id,),
                fetch='one'
            )
            if notification and notification.get('channels'):
                notification['channels'] = json.loads(notification['channels'])
            return notification
        except Exception as e:
            self.logger.error("Failed to get notification", notification_id=notification_id, error=str(e))
            return None

    def update_notification_status(self, notification_id: str, status: str):
        """Update notification status"""
        try:
            self.db.execute_query(
                "UPDATE notifications.notifications SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (status, notification_id)
            )
            self.logger.info("Notification status updated", notification_id=notification_id, status=status)
        except Exception as e:
            self.logger.error("Failed to update notification status", notification_id=notification_id, error=str(e))

    def start_event_consumer(self):
        """Start a background thread to consume Kafka events"""
        def consume_events():
            self.logger.info("Starting event consumer for payment and order events")
            
            while True:
                try:
                    self.events.process_events(
                        topics=['payment-events', 'order-events'],
                        group_id='notification-service-group',
                        handler_func=self.handle_event,
                        max_messages=10
                    )
                    time.sleep(1)
                except Exception as e:
                    self.logger.error("Event consumer error", error=str(e))
                    time.sleep(5)
        
        consumer_thread = threading.Thread(target=consume_events, daemon=True)
        consumer_thread.start()
        self.logger.info("Event consumer thread started")

    def handle_event(self, topic: str, event_data: Dict, key: str):
        """Handle events from Kafka by generating notifications."""
        event_type = event_data.get('event_type')
        order_id = event_data.get('orderId') or event_data.get('order_id')
        
        self.logger.info("Processing event for notification", topic=topic, event_type=event_type, order_id=order_id)
        
        try:
            if event_type == 'OrderCreated':
                self.handle_order_created(event_data)
            elif event_type == 'OrderPaid':
                self.handle_order_paid(event_data)
            elif event_type == 'PaymentFailed':
                self.handle_payment_failed(event_data)
            else:
                self.logger.warning("Unknown event type for notification", event_type=event_type)
            
        except Exception as e:
            self.logger.error(f"Failed to handle {event_type}", error=str(e), order_id=order_id)

    def handle_order_created(self, event_data: Dict):
        """Handle OrderCreated event."""
        user_id = event_data.get('userId', 'anonymous')
        order_id = event_data.get('orderId')
        
        # Ensure order_id is present
        if not order_id:
            self.logger.warning("OrderCreated event missing orderId", event_data=event_data)
            return

        template = self.get_template('OrderCreated')
        if not template:
            return

        try:
            message = template['message_template'].format(**event_data)
            subject = template['title_template'].format(**event_data)
        except KeyError as e:
            self.logger.warning("Template formatting failed", template_type='OrderCreated', missing_key=str(e))
            message = f"Your pizza order #{order_id} has been confirmed."
            subject = "Pizza Order Confirmed"

        self.save_notification(
            user_id=user_id,
            order_id=order_id,
            template_type='OrderCreated',
            subject=subject,
            message=message,
            metadata=event_data
        )

    def handle_order_paid(self, event_data: Dict):
        """Handle OrderPaid event (successful payment)."""
        user_id = event_data.get('user_id', 'anonymous')
        order_id = event_data.get('order_id')
        
        if not order_id:
            self.logger.warning("OrderPaid event missing order_id", event_data=event_data)
            return

        template = self.get_template('OrderPaid')
        if not template:
            return

        try:
            message = template['message_template'].format(**event_data)
            subject = template['title_template'].format(**event_data)
        except KeyError as e:
            self.logger.warning("Template formatting failed", template_type='OrderPaid', missing_key=str(e))
            message = f"Payment for order #{order_id} was successful."
            subject = "Payment Successful"

        self.save_notification(
            user_id=user_id,
            order_id=order_id,
            template_type='OrderPaid',
            subject=subject,
            message=message,
            metadata=event_data
        )

    def handle_payment_failed(self, event_data: Dict):
        """Handle PaymentFailed event."""
        user_id = event_data.get('user_id', 'anonymous')
        order_id = event_data.get('order_id')
        
        if not order_id:
            self.logger.warning("PaymentFailed event missing order_id", event_data=event_data)
            return

        template = self.get_template('PaymentFailed')
        if not template:
            return

        try:
            message = template['message_template'].format(**event_data)
            subject = template['title_template'].format(**event_data)
        except KeyError as e:
            self.logger.warning("Template formatting failed", template_type='PaymentFailed', missing_key=str(e))
            message = f"Payment for order #{order_id} failed."
            subject = "Payment Failed"

        self.save_notification(
            user_id=user_id,
            order_id=order_id,
            template_type='PaymentFailed',
            subject=subject,
            message=message,
            metadata=event_data
        )

    def get_template(self, template_type: str) -> Optional[Dict]:
        """Get notification template from the database."""
        try:
            return self.db.execute_query(
                "SELECT * FROM notifications.notification_templates WHERE type = %s",
                (template_type,),
                fetch='one'
            )
        except Exception as e:
            self.logger.error("Failed to get notification template", template_type=template_type, error=str(e))
            return None

    def save_notification(self, user_id: str, order_id: str, template_type: str, subject: str, message: str, metadata: Dict) -> Optional[str]:
        """Save a generated notification to the database."""
        try:
            notification_id = generate_id('notif_')
            channels = metadata.get('notification_channels', ['EMAIL', 'PUSH']) # Default channels
            
            self.create_notification_record(
                notification_id=notification_id,
                user_id=user_id,
                order_id=order_id,
                subject=subject,
                message=message,
                channels=channels,
                priority='normal',
                template_type=template_type
            )
            self.logger.info("Notification saved to DB", notification_id=notification_id, order_id=order_id)
            return notification_id
        except Exception as e:
            self.logger.error("Failed to save notification", order_id=order_id, error=str(e))
            return None

    def get_timestamp(self) -> str:
        """Get current timestamp in ISO format"""
        return datetime.now(timezone.utc).isoformat()


# ========================================
# Application Entry Point
# ========================================

if __name__ == '__main__':
    try:
        # Create and run service
        service = NotificationService()
        service.logger.info("📢 Starting Notification Service")
        
        # Run in debug mode if specified
        debug_mode = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
        service.run(debug=debug_mode)
        
    except KeyboardInterrupt:
        print("\n🛑 Notification Service stopped by user")
    except Exception as e:
        print(f"❌ Notification Service failed to start: {e}")
        sys.exit(1) 