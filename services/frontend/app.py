"""
Pizza Order System - Frontend Service
Event-Driven Saga Architecture

Manages pizza menu and serves as API gateway for catalog
"""

import os
import sys
import time
from typing import Dict, List, Any
from flask import request, jsonify
from flask_cors import CORS

# Add shared module to path
sys.path.insert(0, '/app/shared')

from base_service import BaseService, generate_id, validate_required_fields, ValidationError


class FrontendService(BaseService):
    """Frontend Service for pizza menu management"""
    
    def __init__(self):
        super().__init__('frontend-service')
        
        # Enable CORS for web UI
        CORS(self.app, origins=['*'])
        
        # Setup routes
        self.setup_routes()
        
        # Initialize database if needed
        self.init_database_with_schema_creation('frontend', 'SELECT 1')
        
        self.logger.info("Frontend Service initialized")
    

    
    def create_sample_data(self):
        """Create sample pizza data if the table is empty."""
        try:
            with self.db.transaction():
                # Insert sample pizzas
                sample_pizzas = [
                    {
                        'id': 'margherita',
                        'name': 'Маргарита',
                        'description': 'Классическая пицца с томатным соусом, моцареллой и базиликом',
                        'price': 59900,  # 599 руб in cents
                        'image_url': '/images/margherita.jpg',
                        'ingredients': ['томатный соус', 'моцарелла', 'базилик'],
                        'available': True
                    },
                    {
                        'id': 'pepperoni',
                        'name': 'Пепперони',
                        'description': 'Острая пицца с пепперони и сыром моцарелла',
                        'price': 69900,  # 699 руб in cents
                        'image_url': '/images/pepperoni.jpg',
                        'ingredients': ['томатный соус', 'моцарелла', 'пепперони'],
                        'available': True
                    },
                    {
                        'id': 'quattro-formaggi',
                        'name': 'Четыре сыра',
                        'description': 'Изысканная пицца с четырьмя видами сыра',
                        'price': 79900,  # 799 руб in cents
                        'image_url': '/images/quattro-formaggi.jpg',
                        'ingredients': ['соус белый', 'моцарелла', 'горгонзола', 'пармезан', 'рикотта'],
                        'available': True
                    }
                ]
                
                with self.db.get_cursor() as cursor:
                    cursor.execute("SET search_path TO frontend, public")
                    # Check if pizzas already exist
                    cursor.execute("SELECT COUNT(*) FROM frontend.pizzas")
                    if cursor.fetchone()[0] > 0:
                        self.logger.info("Sample pizza data already exists.")
                        return

                    for pizza in sample_pizzas:
                        cursor.execute("""
                            INSERT INTO frontend.pizzas (id, name, description, price, image_url, ingredients, available)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (id) DO NOTHING
                        """, (
                            pizza['id'],
                            pizza['name'],
                            pizza['description'],
                            pizza['price'],
                            pizza['image_url'],
                            pizza['ingredients'],
                            pizza['available']
                        ))
                
                self.logger.info("Sample pizza data created")
                
        except Exception as e:
            self.logger.error("Failed to create sample data", error=str(e))
    
    def setup_routes(self):
        """Setup API routes for frontend service"""
        
        @self.app.route('/api/v1/menu', methods=['GET'])
        def get_menu():
            """Get pizza menu with optional filtering"""
            try:
                # Get query parameters
                available_only = request.args.get('available', 'true').lower() == 'true'
                category = request.args.get('category')
                
                # Build query
                query = "SELECT * FROM frontend.pizzas"
                params = []
                
                if available_only:
                    query += " WHERE available = %s"
                    params.append(True)
                
                query += " ORDER BY name"
                
                # Execute query
                pizzas = self.db.execute_query(query, tuple(params), fetch='all')
                
                # Log the request
                self.logger.info(
                    "Menu requested",
                    pizza_count=len(pizzas),
                    available_only=available_only,
                    user_agent=request.headers.get('User-Agent', 'unknown')
                )
                
                # Record business metrics
                self.metrics.record_business_event('menu_request', 'success')
                
                return jsonify({
                    'success': True,
                    'pizzas': pizzas,
                    'total': len(pizzas),
                    'timestamp': self.get_timestamp()
                })
                
            except Exception as e:
                self.logger.error("Failed to get menu", error=str(e))
                self.metrics.record_business_event('menu_request', 'failed')
                
                return jsonify({
                    'success': False,
                    'error': 'Failed to load menu',
                    'message': str(e)
                }), 500
        
        @self.app.route('/api/v1/menu/<pizza_id>', methods=['GET'])
        def get_pizza(pizza_id: str):
            """Get specific pizza by ID"""
            try:
                pizza = self.db.execute_query(
                    "SELECT * FROM frontend.pizzas WHERE id = %s",
                    (pizza_id,),
                    fetch='one'
                )
                
                if not pizza:
                    return jsonify({
                        'success': False,
                        'error': 'Pizza not found'
                    }), 404
                
                self.logger.info("Pizza requested", pizza_id=pizza_id)
                self.metrics.record_business_event('pizza_detail_request', 'success')
                
                return jsonify({
                    'success': True,
                    'pizza': pizza,
                    'timestamp': self.get_timestamp()
                })
                
            except Exception as e:
                self.logger.error("Failed to get pizza", pizza_id=pizza_id, error=str(e))
                self.metrics.record_business_event('pizza_detail_request', 'failed')
                
                return jsonify({
                    'success': False,
                    'error': 'Failed to load pizza details'
                }), 500
        
        @self.app.route('/api/v1/menu', methods=['POST'])
        def add_pizza():
            """Add new pizza to menu (admin function)"""
            try:
                data = request.get_json()
                
                # Validate required fields
                required_fields = ['id', 'name', 'description', 'price', 'ingredients']
                missing_fields = validate_required_fields(data, required_fields)
                
                if missing_fields:
                    raise ValidationError(f"Missing required fields: {', '.join(missing_fields)}")
                
                # Validate price is positive
                if data['price'] <= 0:
                    raise ValidationError("Price must be positive")
                
                # Set defaults
                data.setdefault('image_url', f"/images/{data['id']}.jpg")
                data.setdefault('available', True)
                
                # Insert pizza
                with self.db.transaction():
                    with self.db.get_cursor() as cursor:
                        cursor.execute("SET search_path TO frontend, public")
                        cursor.execute("""
                            INSERT INTO frontend.pizzas (id, name, description, price, image_url, ingredients, available)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (id) DO UPDATE SET
                                name = EXCLUDED.name,
                                description = EXCLUDED.description,
                                price = EXCLUDED.price,
                                image_url = EXCLUDED.image_url,
                                ingredients = EXCLUDED.ingredients,
                                available = EXCLUDED.available
                        """, (
                            data['id'],
                            data['name'],
                            data['description'],
                            data['price'],
                            data['image_url'],
                            data['ingredients'],
                            data['available']
                        ))
                
                self.logger.info("Pizza added to menu", pizza_id=data['id'], pizza_name=data['name'])
                self.metrics.record_business_event('pizza_added', 'success')
                
                return jsonify({
                    'success': True,
                    'message': 'Pizza added successfully',
                    'pizza_id': data['id']
                }), 201
                
            except ValidationError as e:
                self.logger.warning("Pizza validation failed", error=str(e))
                return jsonify({
                    'success': False,
                    'error': 'Validation error',
                    'message': str(e)
                }), 400
                
            except Exception as e:
                self.logger.error("Failed to add pizza", error=str(e))
                self.metrics.record_business_event('pizza_added', 'failed')
                
                return jsonify({
                    'success': False,
                    'error': 'Failed to add pizza'
                }), 500
        
        @self.app.route('/api/v1/menu/<pizza_id>', methods=['PUT'])
        def update_pizza(pizza_id: str):
            """Update existing pizza in menu (admin function)"""
            try:
                data = request.get_json()
                
                # Build update query dynamically
                update_fields = []
                params = []
                
                allowed_fields = ['name', 'description', 'price', 'image_url', 'ingredients', 'available']
                
                for field in allowed_fields:
                    if field in data:
                        update_fields.append(f"{field} = %s")
                        params.append(data[field])
                
                if not update_fields:
                    raise ValidationError("No valid fields to update")
                
                # Add pizza_id to params
                params.append(pizza_id)
                
                # Execute update
                with self.db.transaction():
                    with self.db.get_cursor() as cursor:
                        cursor.execute("SET search_path TO frontend, public")
                        cursor.execute(f"""
                            UPDATE frontend.pizzas 
                            SET {', '.join(update_fields)}, updated_at = CURRENT_TIMESTAMP
                            WHERE id = %s
                        """, tuple(params))
                        
                        if cursor.rowcount == 0:
                            raise ValidationError("Pizza not found")
                
                self.logger.info("Pizza updated", pizza_id=pizza_id)
                self.metrics.record_business_event('pizza_updated', 'success')
                
                return jsonify({
                    'success': True,
                    'message': 'Pizza updated successfully'
                })
                
            except ValidationError as e:
                self.logger.warning("Pizza update validation failed", pizza_id=pizza_id, error=str(e))
                return jsonify({
                    'success': False,
                    'error': 'Validation error',
                    'message': str(e)
                }), 400
                
            except Exception as e:
                self.logger.error("Failed to update pizza", pizza_id=pizza_id, error=str(e))
                self.metrics.record_business_event('pizza_updated', 'failed')
                
                return jsonify({
                    'success': False,
                    'error': 'Failed to update pizza'
                }), 500
        
        @self.app.route('/api/v1/menu/<pizza_id>', methods=['DELETE'])
        def delete_pizza(pizza_id: str):
            """Delete a pizza from the menu (admin function)"""
            try:
                with self.db.get_cursor(commit=True) as cursor:
                    cursor.execute("SET search_path TO frontend, public")
                    cursor.execute(
                        "DELETE FROM frontend.pizzas WHERE id = %s",
                        (pizza_id,)
                    )
                    
                    if cursor.rowcount == 0:
                        return jsonify({
                            'success': False,
                            'error': 'Pizza not found'
                        }), 404
                
                self.logger.info("Pizza deleted", pizza_id=pizza_id)
                self.metrics.record_business_event('pizza_deleted', 'success')
                
                return jsonify({
                    'success': True,
                    'message': 'Pizza deleted successfully'
                })
                
            except Exception as e:
                self.logger.error("Failed to delete pizza", pizza_id=pizza_id, error=str(e))
                self.metrics.record_business_event('pizza_deleted', 'failed')
                
                return jsonify({
                    'success': False,
                    'error': 'Failed to delete pizza'
                }), 500
        
        @self.app.route('/api/v1/stats', methods=['GET'])
        def get_stats():
            """Get service statistics"""
            try:
                with self.db.get_cursor() as cursor:
                    cursor.execute("SET search_path TO frontend, public")
                    cursor.execute("SELECT COUNT(*) FROM frontend.pizzas")
                    pizza_count = cursor.fetchone()[0]

                self.logger.info("Stats requested")
                self.metrics.record_business_event('stats_request', 'success')
                
                return jsonify({
                    'service': self.service_name,
                    'pizza_count': pizza_count,
                    'timestamp': self.get_timestamp()
                })
                
            except Exception as e:
                self.logger.error("Failed to get stats", error=str(e))
                self.metrics.record_business_event('stats_request', 'failed')
                
                return jsonify({
                    'success': False,
                    'error': 'Failed to retrieve stats'
                }), 500
    
    def get_timestamp(self) -> str:
        """Get current timestamp in ISO 8601 format"""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


# ========================================
# Application Entry Point
# ========================================

if __name__ == '__main__':
    try:
        # Create and run service
        service = FrontendService()
        service.logger.info("🍕 Starting Frontend Service")
        
        # Run in debug mode if specified
        debug_mode = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
        service.run(debug=debug_mode)
        
    except KeyboardInterrupt:
        print("\n🛑 Frontend Service stopped by user")
    except Exception as e:
        print(f"❌ Frontend Service failed to start: {e}")
        sys.exit(1) 