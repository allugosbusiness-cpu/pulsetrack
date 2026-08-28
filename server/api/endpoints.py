"""
Consolidated function-based API endpoints (missions, locations, trail/audit,
smart alerts, and production hardening). The original platform split these
across several small modules; they are combined here while preserving every
public URL route and function name.
"""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.utils import timezone
from django.db import connection
from .models import FleetDriver, FleetTruck, FleetMission, TruckLocation
from .osrm_service import compute_route_geometry
import logging
from .geocoding_proxy import search_nominatim, reverse_geocode_nominatim
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.db.models import Q, Count, Min, Max
from datetime import timedelta
from .models import FleetActivity, FleetTruck, FleetDriver, FleetMission, TruckLocation
import json
import uuid
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Q
from .models import Alert, FleetTruck, FleetDriver, FleetMission, TruckLocation, FleetActivity
from django.db import connection, transaction

logger = logging.getLogger(__name__)

GEOFENCE_RADIUS_METERS = 100
OVERSPEED_THRESHOLD = 100
DELAY_MINUTES = 5
DELAY_SPEED_THRESHOLD = 3

@api_view(['GET'])
@permission_classes([AllowAny])
def get_available_missions(request, driver_id):
    """
    Get all available missions for a driver that are ready to start
    Filters by truck assignment and mission status
    """
    try:
        driver = FleetDriver.objects.get(id=driver_id)
        truck = driver.truck
        if not truck:
            return Response({'missions': [], 'message': 'Driver has not been assigned to a truck yet'}, status=status.HTTP_200_OK)
        missions = FleetMission.objects.filter(truck=truck, status__in=['planned', 'assigned']).order_by('-created_at')
        missions_data = []
        for mission in missions:
            missions_data.append({'id': str(mission.id), 'mission_number': mission.mission_number, 'status': mission.status, 'origin': mission.origin if isinstance(mission.origin, dict) else {'lat': 0, 'lng': 0}, 'destination': mission.destination if isinstance(mission.destination, dict) else {'lat': 0, 'lng': 0}, 'distance_total_m': float(mission.distance_total_m), 'cargo': mission.cargo if mission.cargo else {}, 'created_at': mission.created_at.isoformat() if mission.created_at else None})
        return Response({'driver_id': str(driver.id), 'driver_name': driver.get_display_name(), 'truck_id': str(truck.id), 'truck_name': truck.truck_identifier, 'missions': missions_data, 'total_count': len(missions_data)}, status=status.HTTP_200_OK)
    except FleetDriver.DoesNotExist:
        return Response({'error': 'Driver not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['GET'])
@permission_classes([AllowAny])
def mission_route_geometry(request, mission_id):
    """
    Get OSRM route geometry for a mission's origin → destination.
    Used by the GlobalMap to display the planned route per truck/mission.
    """
    try:
        mission = FleetMission.objects.filter(id=mission_id).first()
        if not mission:
            return Response({'error': 'Mission not found'}, status=status.HTTP_404_NOT_FOUND)
        origin = mission.get_origin_coords()
        destination = mission.get_destination_coords()
        if not origin or not destination:
            return Response({'error': 'Mission missing origin or destination coordinates'}, status=status.HTTP_400_BAD_REQUEST)
        route_data = compute_route_geometry(origin_lat=origin['lat'], origin_lng=origin['lon'], dest_lat=destination['lat'], dest_lng=destination['lon'])
        if not route_data:
            import json
            fallback_geometry = {'type': 'LineString', 'coordinates': [[origin['lon'], origin['lat']], [destination['lon'], destination['lat']]]}
            return Response({'mission_id': str(mission.id), 'mission_number': mission.mission_number, 'geometry': fallback_geometry, 'distance': None, 'duration': None, 'fallback': True, 'message': 'OSRM route unavailable, using straight-line'})
        return Response({'mission_id': str(mission.id), 'mission_number': mission.mission_number, 'geometry': route_data['geometry'], 'distance': route_data.get('distance'), 'duration': route_data.get('duration')})
    except Exception as e:
        logger.error(f'Error fetching mission route geometry: {str(e)}')
        import traceback
        logger.error(traceback.format_exc())
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

def check_mission_geofence(mission, current_lat, current_lon):
    """
    Check if a truck is within geofence of mission destination.
    If within GEOFENCE_RADIUS_METERS, auto-complete the mission.
    """
    try:
        destination = mission.get_destination_coords()
        if not destination:
            return False
        from math import radians, cos, sin, asin, sqrt
        dest_lat = destination['lat']
        dest_lon = destination['lon']
        lon1_rad = radians(current_lon)
        lat1_rad = radians(current_lat)
        lon2_rad = radians(dest_lon)
        lat2_rad = radians(dest_lat)
        dlon = lon2_rad - lon1_rad
        dlat = lat2_rad - lat1_rad
        a = sin(dlat / 2) ** 2 + cos(lat1_rad) * cos(lat2_rad) * sin(dlon / 2) ** 2
        c = 2 * asin(sqrt(a))
        distance_m = 6371000 * c
        if distance_m <= GEOFENCE_RADIUS_METERS:
            if mission.status in ['enroute', 'in_progress']:
                mission.status = 'completed'
                mission.completed_at = timezone.now()
                mission.delivered_at = timezone.now()
                mission.progress_pct = 100
                mission.save(update_fields=['status', 'completed_at', 'delivered_at', 'progress_pct'])
                logger.info(f'✅ Mission {mission.mission_number} auto-completed on arrival (geofence)')
                return True
        return False
    except Exception as e:
        logger.error(f'Geofence check error: {str(e)}')
        return False

def _safe_create_mission(data):
    """
    Safely create a mission even when optional columns are missing from the database.
    This handles the case where max_speed, avg_speed, and compressed_trail columns
    don't exist in the production database.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("\n                SELECT column_name \n                FROM information_schema.columns \n                WHERE table_name = 'fleet_missions' \n                AND column_name IN ('max_speed', 'avg_speed', 'compressed_trail')\n            ")
            existing_columns = {row[0] for row in cursor.fetchall()}
        create_data = data.copy()
        if 'max_speed' not in existing_columns:
            create_data.pop('max_speed', None)
        if 'avg_speed' not in existing_columns:
            create_data.pop('avg_speed', None)
        if 'compressed_trail' not in existing_columns:
            create_data.pop('compressed_trail', None)
        mission = FleetMission.objects.create(**create_data)
        if 'max_speed' not in existing_columns:
            mission.max_speed = 0
        if 'avg_speed' not in existing_columns:
            mission.avg_speed = 0
        if 'compressed_trail' not in existing_columns:
            mission.compressed_trail = []
        if any((col not in existing_columns for col in ['max_speed', 'avg_speed', 'compressed_trail'])):
            mission.save(update_fields=['max_speed', 'avg_speed', 'compressed_trail'])
        return mission
    except Exception as e:
        raise Exception(f'Failed to create mission: {str(e)}')

@api_view(['POST'])
@permission_classes([AllowAny])
def create_mission(request):
    """
    Create a new mission with graceful handling of missing database columns.
    This endpoint works even when max_speed, avg_speed, and compressed_trail columns
    are missing from the production database.
    """
    try:
        mission_number = request.data.get('mission_number')
        if not mission_number:
            return Response({'error': 'mission_number is required'}, status=status.HTTP_400_BAD_REQUEST)
        if FleetMission.objects.filter(mission_number=mission_number).exists():
            return Response({'error': f'Mission {mission_number} already exists'}, status=status.HTTP_400_BAD_REQUEST)
        mission_data = {'mission_number': mission_number, 'status': request.data.get('status', 'planned'), 'priority': request.data.get('priority', 'normal'), 'origin': request.data.get('origin', {}), 'destination': request.data.get('destination', {}), 'distance_total_m': request.data.get('distance_total_m', 0), 'progress_pct': request.data.get('progress_pct', 0), 'cargo': request.data.get('cargo', {}), 'mission_date': request.data.get('mission_date')}
        truck_identifier = request.data.get('truck')
        if truck_identifier:
            truck = FleetTruck.objects.filter(truck_identifier=truck_identifier).first()
            if not truck:
                return Response({'error': f'Truck {truck_identifier} not found'}, status=status.HTTP_400_BAD_REQUEST)
            mission_data['truck'] = truck
        driver_identifier = request.data.get('driver')
        if driver_identifier:
            driver = FleetDriver.objects.filter(phone_number=driver_identifier).first()
            if not driver:
                return Response({'error': f'Driver {driver_identifier} not found'}, status=status.HTTP_400_BAD_REQUEST)
            mission_data['driver'] = driver
        mission = _safe_create_mission(mission_data)
        return Response({'success': True, 'mission_id': str(mission.id), 'mission_number': mission.mission_number, 'status': mission.status, 'origin': mission.origin, 'destination': mission.destination, 'truck_name': mission.truck.truck_identifier if mission.truck else None, 'driver_name': mission.driver.get_display_name() if mission.driver else None, 'max_speed': mission.max_speed, 'avg_speed': mission.avg_speed, 'compressed_trail': mission.compressed_trail, 'created_at': mission.created_at.isoformat(), 'message': f'Mission {mission.mission_number} created successfully'}, status=status.HTTP_201_CREATED)
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

@api_view(['POST'])
@permission_classes([AllowAny])
def start_mission_tracking(request):
    """
    Start tracking for a mission
    Accepts either mission_id or mission_number
    Optional: latitude, longitude for driver's current location
    """
    try:
        driver_id = request.data.get('driver_id')
        mission_id = request.data.get('mission_id')
        mission_number = request.data.get('mission_number')
        current_latitude = request.data.get('latitude')
        current_longitude = request.data.get('longitude')
        if not driver_id:
            return Response({'error': 'driver_id required'}, status=status.HTTP_400_BAD_REQUEST)
        mission = None
        if mission_id:
            mission = FleetMission.objects.get(id=mission_id)
        elif mission_number:
            mission = FleetMission.objects.get(mission_number=mission_number)
        else:
            return Response({'error': 'mission_id or mission_number required'}, status=status.HTTP_400_BAD_REQUEST)
        driver = FleetDriver.objects.get(id=driver_id)
        if driver.truck != mission.truck:
            return Response({'error': "Driver is not assigned to this mission's truck"}, status=status.HTTP_403_FORBIDDEN)
        mission.status = 'enroute'
        mission.driver = driver
        mission.started_at = timezone.now()
        if current_latitude is not None and current_longitude is not None:
            try:
                mission.origin = {'lat': float(current_latitude), 'lon': float(current_longitude)}
                import logging
                logger = logging.getLogger(__name__)
                logger.info(f'Mission {mission.id} initialized with driver current location: ({current_latitude}, {current_longitude})')
            except (ValueError, TypeError):
                pass
        mission.save()
        from django.core.cache import cache
        cache.set(f'mission_tracking_{mission.id}', {'mission_id': str(mission.id), 'driver_id': str(driver.id), 'truck_id': str(mission.truck.id), 'started_at': timezone.now().isoformat(), 'tracking_enabled': True}, timeout=None)
        return Response({'success': True, 'mission_id': str(mission.id), 'mission_number': mission.mission_number, 'status': mission.status, 'origin': mission.origin, 'destination': mission.destination, 'driver_name': driver.get_display_name(), 'tracking_id': str(mission.id), 'message': f'Started tracking mission {mission.mission_number}'}, status=status.HTTP_200_OK)
    except FleetMission.DoesNotExist:
        return Response({'error': 'Mission not found'}, status=status.HTTP_404_NOT_FOUND)
    except FleetDriver.DoesNotExist:
        return Response({'error': 'Driver not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['GET'])
@permission_classes([AllowAny])
def reverse_geocode(request):
    """
    Reverse geocode: Find the nearest named location for given lat/lon.
    Uses OSM Nominatim via the proxy (cached, rate-limited).

    Query params: lat, lon
    Returns: { name, lat, lon, type, display_name }
    """
    try:
        lat = float(request.query_params.get('lat', 0))
        lon = float(request.query_params.get('lon', 0))
        if not lat or not lon:
            return Response({'error': 'lat and lon query parameters are required'}, status=status.HTTP_400_BAD_REQUEST)
        result = reverse_geocode_nominatim(lat, lon)
        if result:
            return Response(result, status=status.HTTP_200_OK)
        else:
            return Response({'name': f'Approx: {round(lat, 4)}, {round(lon, 4)}', 'lat': lat, 'lon': lon, 'type': 'unknown', 'source': 'approximate'}, status=status.HTTP_200_OK)
    except (TypeError, ValueError) as e:
        return Response({'error': f'Invalid coordinates: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['GET'])
@permission_classes([AllowAny])
def location_autocomplete(request):
    """
    Autocomplete location search using OSM Nominatim (free, no API key needed).
    Query params: q (search query)
    Returns: { results: [{name, lat, lon, type, source}] }

    All results come from OSM Nominatim — no hardcoded location lists.
    Server-side caching + rate limiting protects Nominatim's free tier.
    Client-side caching (localStorage) further reduces API calls.
    """
    try:
        query = request.query_params.get('q', '').strip()
        source = request.query_params.get('source', 'auto')
        if not query or len(query) < 2:
            return Response({'results': []}, status=status.HTTP_200_OK)
        results = []
        if source in ('auto', 'nominatim'):
            try:
                nominatim_results = search_nominatim(query, limit=10)
                if nominatim_results:
                    results = nominatim_results
            except Exception as e:
                logger.warning(f'Nominatim search failed: {e}')
        results = results[:15]
        return Response({'results': results, 'count': len(results), 'source': source, 'query': query}, status=status.HTTP_200_OK)
    except Exception as e:
        logger.error(f'Location autocomplete error: {str(e)}')
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@require_http_methods(['GET'])
def truck_trail_audit(request, truck_id):
    """
    GET /api/v1/trucks/<truck_id>/trail-audit/?days=30&limit=500

    Returns the FULL GPS trail + activity audit logs for a truck.
    This is used by the web app to draw the trail of where the mobile app has been.

    Response:
    {
        "truck_id": "uuid",
        "truck_identifier": "Truck-001",
        "plate": "ABC123",
        "driver_name": "John Doe",
        "trail": [
            {
                "latitude": -18.975,
                "longitude": 32.655,
                "speed": 45.5,
                "accuracy": 10,
                "altitude": 1200,
                "timestamp": "2026-05-11T04:15:00Z",
                "sequence": 1
            }
        ],
        "audit_log": [
            {
                "id": "uuid",
                "activity_type": "trail_recorded",
                "activity_type_display": "Trail Recorded",
                "location": "Mutare CBD",
                "speed_kmh": 45.5,
                "distance_m": 1234.5,
                "timestamp": "2026-05-11T04:15:00Z",
                "notes": "Trail segment recorded: 15 points"
            }
        ],
        "stats": {
            "total_points": 1500,
            "total_distance_km": 245.5,
            "avg_speed": 55.2,
            "max_speed": 95.0,
            "start_time": "2026-05-10T08:00:00Z",
            "end_time": "2026-05-11T04:15:00Z",
            "duration_hours": 20.25,
            "trail_segments": 12
        },
        "count": 1500
    }
    """
    try:
        try:
            truck = FleetTruck.objects.get(id=truck_id)
        except FleetTruck.DoesNotExist:
            truck = FleetTruck.objects.filter(truck_identifier=truck_id).first()
        except (ValueError, TypeError):
            truck = FleetTruck.objects.filter(truck_identifier=truck_id).first()
        if not truck:
            return JsonResponse({'error': f'Truck "{truck_id}" not found', 'truck_id': truck_id, 'trail': [], 'audit_log': [], 'count': 0}, status=404)
        days = int(request.GET.get('days', 30))
        limit = int(request.GET.get('limit', 500))
        try:
            limit = min(limit, 5000)
        except (TypeError, ValueError):
            limit = 500
        start_date = timezone.now() - timedelta(days=days)
        locations = TruckLocation.objects.filter(truck=truck, timestamp__gte=start_date).order_by('-timestamp')[:limit]
        trail = []
        reversed_locations = list(reversed(locations))
        for i, loc in enumerate(reversed_locations):
            trail_point = {'latitude': float(loc.latitude), 'longitude': float(loc.longitude), 'speed': float(loc.speed), 'accuracy': float(loc.accuracy), 'altitude': float(loc.altitude), 'timestamp': loc.timestamp.isoformat(), 'sequence': i + 1}
            if i < len(reversed_locations) - 1:
                next_loc = reversed_locations[i + 1]
                from math import atan2, degrees, sqrt
                dlat = float(next_loc.latitude) - float(loc.latitude)
                dlon = float(next_loc.longitude) - float(loc.longitude)
                bearing = degrees(atan2(dlon, dlat)) % 360
                trail_point['bearing'] = round(bearing, 1)
            trail.append(trail_point)
        latest_location = TruckLocation.objects.filter(truck=truck).order_by('-timestamp').first()
        driver_name = None
        if latest_location and latest_location.driver:
            driver_name = latest_location.driver.get_display_name()
        audit_logs = FleetActivity.objects.filter(truck=truck, timestamp__gte=start_date).order_by('-timestamp')
        trail_segments_total = audit_logs.filter(activity_type='trail_recorded').count()
        audit_logs = audit_logs[:100]
        audit_log = []
        for activity in audit_logs:
            audit_entry = {'id': str(activity.id), 'activity_type': activity.activity_type, 'activity_type_display': activity.get_activity_type_display(), 'activity_category': activity.activity_category, 'location_lat': float(activity.location_lat) if activity.location_lat else None, 'location_lon': float(activity.location_lon) if activity.location_lon else None, 'location_name': activity.location_name or activity.display_location, 'speed_kmh': float(activity.speed_kmh) if activity.speed_kmh else None, 'distance_m': float(activity.distance_m) if activity.distance_m else None, 'timestamp': activity.timestamp.isoformat(), 'notes': activity.notes, 'alert_level': activity.alert_level, 'is_critical': activity.is_critical}
            audit_log.append(audit_entry)
        stats = {}
        if trail:
            coords = [(p['latitude'], p['longitude']) for p in trail]
            speeds = [p['speed'] for p in trail]
            total_distance_km = 0
            for i in range(1, len(coords)):
                from math import radians, sin, cos, sqrt, asin
                lat1, lon1 = coords[i - 1]
                lat2, lon2 = coords[i]
                R = 6371
                dlat = radians(lat2 - lat1)
                dlon = radians(lon2 - lon1)
                a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
                c = 2 * asin(sqrt(a))
                total_distance_km += R * c
            stats = {'total_points': len(trail), 'total_distance_km': round(total_distance_km, 2), 'avg_speed': round(sum(speeds) / len(speeds), 2) if speeds else 0, 'max_speed': round(max(speeds), 2) if speeds else 0, 'start_time': trail[0]['timestamp'] if trail else None, 'end_time': trail[-1]['timestamp'] if trail else None, 'duration_hours': round((timezone.datetime.fromisoformat(trail[-1]['timestamp'].replace('Z', '+00:00')) - timezone.datetime.fromisoformat(trail[0]['timestamp'].replace('Z', '+00:00'))).total_seconds() / 3600, 2) if len(trail) >= 2 else 0, 'trail_segments': trail_segments_total}
        logger.info(f'🚚 Trail audit for {truck.truck_identifier}: {len(trail)} trail points, {len(audit_log)} audit entries')
        response_data = {'truck_id': str(truck.id), 'truck_identifier': truck.truck_identifier, 'plate': truck.plate, 'driver_name': driver_name, 'trail': trail, 'audit_log': audit_log, 'stats': stats, 'count': len(trail), 'days': days}
        return JsonResponse(response_data, status=200)
    except Exception as e:
        logger.error(f'❌ Trail audit error: {str(e)}')
        import traceback
        logger.error(traceback.format_exc())
        return JsonResponse({'error': str(e)}, status=400)

@require_http_methods(['GET'])
def truck_trail_summary(request, truck_id):
    """
    GET /api/v1/trucks/<truck_id>/trail-summary/?days=7

    Lightweight summary of trail data for dashboard display.
    Returns just stats without the full trail points.
    """
    try:
        try:
            truck = FleetTruck.objects.get(id=truck_id)
        except FleetTruck.DoesNotExist:
            truck = FleetTruck.objects.filter(truck_identifier=truck_id).first()
        if not truck:
            return JsonResponse({'error': f'Truck "{truck_id}" not found'}, status=404)
        days = int(request.GET.get('days', 7))
        start_date = timezone.now() - timedelta(days=days)
        location_stats = TruckLocation.objects.filter(truck=truck, timestamp__gte=start_date).aggregate(total_points=Count('id'), earliest=Min('timestamp'), latest=Max('timestamp'))
        audit_count = FleetActivity.objects.filter(truck=truck, timestamp__gte=start_date, activity_type='trail_recorded').count()
        return JsonResponse({'truck_id': str(truck.id), 'truck_identifier': truck.truck_identifier, 'plate': truck.plate, 'stats': {'total_gps_points': location_stats.get('total_points', 0), 'trail_segments_recorded': audit_count, 'earliest_timestamp': location_stats.get('earliest').isoformat() if location_stats.get('earliest') else None, 'latest_timestamp': location_stats.get('latest').isoformat() if location_stats.get('latest') else None}}, status=200)
    except Exception as e:
        logger.error(f'❌ Trail summary error: {str(e)}')
        return JsonResponse({'error': str(e)}, status=400)

@require_http_methods(['GET'])
def all_trucks_trail_summary(request):
    """
    GET /api/v1/trucks/trail-summary/?days=7

    Summary of trail data for ALL trucks.
    Used by dashboard to show which trucks have trail data.
    """
    try:
        days = int(request.GET.get('days', 7))
        start_date = timezone.now() - timedelta(days=days)
        trucks = FleetTruck.objects.all()
        summaries = []
        for truck in trucks:
            point_count = TruckLocation.objects.filter(truck=truck, timestamp__gte=start_date).count()
            latest = TruckLocation.objects.filter(truck=truck, timestamp__gte=start_date).order_by('-timestamp').first()
            summaries.append({'truck_id': str(truck.id), 'truck_identifier': truck.truck_identifier, 'plate': truck.plate, 'status': truck.status, 'trail_points': point_count, 'has_trail': point_count > 0, 'last_latitude': float(latest.latitude) if latest else None, 'last_longitude': float(latest.longitude) if latest else None, 'last_timestamp': latest.timestamp.isoformat() if latest else None})
        return JsonResponse({'count': len(summaries), 'trucks': summaries, 'days': days}, status=200)
    except Exception as e:
        logger.error(f'❌ All trucks trail summary error: {str(e)}')
        return JsonResponse({'error': str(e)}, status=400)

def log_alert_to_activity(truck, driver, mission, alert_type, message, latitude, longitude, speed, severity='high'):
    """Log any alert to the FleetActivity table for the audit trail view."""
    try:
        category_map = {'overspeed': 'speed', 'delayed': 'trail', 'driver_alert': 'driver', 'off_route': 'breach', 'maintenance': 'maintenance'}
        category = category_map.get(alert_type, 'alert')
        FleetActivity.objects.create(fleet_id=truck.fleet_id if truck else driver.fleet_id if driver else None, truck=truck, driver=driver, mission=mission, activity_type='alert', activity_category=category, location_lat=latitude, location_lon=longitude, speed_kmh=speed, alert_level=severity, breach_type='speeding' if alert_type == 'overspeed' else alert_type, violation_details=message[:500], notes=f'{alert_type}: {message[:200]}', is_critical=severity in ['high', 'critical'], timestamp=timezone.now())
    except Exception as e:
        logger.warning(f'Failed to log alert to activity trail: {e}')

@csrf_exempt
@require_http_methods(['POST'])
def check_and_create_alerts(request):
    """POST /api/v1/alerts/check/

    Called after each location update. Checks:
    1. Overspeeding (speed > 100 km/h)
    2. Delayed (stopped > 5 minutes)
    Returns active alerts.
    """
    try:
        data = json.loads(request.body) if request.body else {}
        driver_id = data.get('driver_id')
        truck_id = data.get('truck_id')
        mission_id = data.get('mission_id')
        current_speed = float(data.get('speed', 0))
        latitude = float(data.get('latitude', 0))
        longitude = float(data.get('longitude', 0))
        alerts_created = []
        truck = None
        driver = None
        mission = None
        if truck_id:
            try:
                truck = FleetTruck.objects.get(id=truck_id)
            except FleetTruck.DoesNotExist:
                pass
        if driver_id:
            try:
                driver = FleetDriver.objects.get(id=driver_id)
            except FleetDriver.DoesNotExist:
                pass
        if mission_id:
            try:
                mission = FleetMission.objects.get(id=mission_id)
            except FleetMission.DoesNotExist:
                pass
        if current_speed > OVERSPEED_THRESHOLD:
            existing = Alert.objects.filter(alert_type='overspeed', is_resolved=False)
            if truck:
                existing = existing.filter(truck=truck)
            if not existing.exists():
                truck_name = truck.truck_identifier if truck else 'Truck'
                msg = f'⚠️ OVERSPEED: {truck_name} at {current_speed:.0f} km/h (limit: {OVERSPEED_THRESHOLD})'
                alert = Alert.objects.create(id=uuid.uuid4(), truck=truck, driver=driver, alert_type='overspeed', severity='high', message=msg, location_lat=latitude, location_lon=longitude, speed_kmh=current_speed)
                alerts_created.append({'id': str(alert.id), 'type': 'overspeed', 'severity': 'high', 'message': msg, 'speed': current_speed})
                log_alert_to_activity(truck, driver, mission, 'overspeed', msg, latitude, longitude, current_speed, 'high')
                logger.warning(f'🚨 OVERSPEED: {truck_name} at {current_speed} km/h')
        if current_speed < DELAY_SPEED_THRESHOLD and truck:
            recent = TruckLocation.objects.filter(truck=truck).order_by('-timestamp')[:20]
            if recent.count() >= 5:
                stopped_count = sum((1 for loc in recent if float(loc.speed) < DELAY_SPEED_THRESHOLD))
                if stopped_count >= 5:
                    first_stopped = list(recent)[-1]
                    duration = (timezone.now() - first_stopped.timestamp).total_seconds() / 60
                    if duration >= DELAY_MINUTES:
                        existing = Alert.objects.filter(alert_type='delayed', is_resolved=False, truck=truck)
                        if not existing.exists():
                            mi = f' on mission {mission.mission_number}' if mission else ''
                            msg = f'⏰ DELAYED: {truck.truck_identifier} stopped {duration:.0f} min{mi}'
                            alert = Alert.objects.create(id=uuid.uuid4(), truck=truck, driver=driver, mission=mission, alert_type='delayed', severity='medium', message=msg, location_lat=latitude, location_lon=longitude, speed_kmh=current_speed)
                            alerts_created.append({'id': str(alert.id), 'type': 'delayed', 'severity': 'medium', 'message': msg, 'stopped_minutes': round(duration, 1)})
                            log_alert_to_activity(truck, driver, mission, 'delayed', msg, latitude, longitude, current_speed, 'medium')
                            logger.warning(f'⏰ DELAY: {truck.truck_identifier} stopped {duration:.0f} min')
        active_qs = Alert.objects.filter(is_resolved=False).order_by('-created_at')
        if truck:
            # Django forbids .filter() once a queryset has been sliced, so filter first.
            active_qs = active_qs.filter(Q(truck=truck) | Q(truck__isnull=True))
        active = active_qs[:20]
        active_alerts = [{'id': str(a.id), 'type': a.alert_type, 'severity': a.severity, 'message': a.message, 'speed_kmh': float(a.speed_kmh) if a.speed_kmh else None, 'created_at': a.created_at.isoformat(), 'is_resolved': a.is_resolved} for a in active]
        return JsonResponse({'success': True, 'alerts_created': alerts_created, 'active_alerts': active_alerts, 'alert_count': len(active_alerts)})
    except Exception as e:
        logger.error(f'Alert check error: {str(e)}')
        return JsonResponse({'error': str(e)}, status=400)

@csrf_exempt
@require_http_methods(['POST'])
def driver_send_alert(request):
    """POST /api/v1/alerts/driver-send/

    Driver sends custom alert to fleet manager. Also logged to activity trail.
    """
    try:
        data = json.loads(request.body) if request.body else {}
        driver_id = data.get('driver_id')
        truck_id = data.get('truck_id')
        message = data.get('message', '').strip()
        alert_category = data.get('alert_category', 'other')
        latitude = float(data.get('latitude', 0))
        longitude = float(data.get('longitude', 0))
        speed = float(data.get('speed', 0))
        if not message:
            return JsonResponse({'error': 'Message is required'}, status=400)
        if len(message) < 5:
            return JsonResponse({'error': 'Message must be at least 5 characters'}, status=400)
        if len(message) > 500:
            return JsonResponse({'error': 'Message too long (max 500 chars)'}, status=400)
        driver = None
        truck = None
        if driver_id:
            try:
                driver = FleetDriver.objects.get(id=driver_id)
            except:
                pass
        if truck_id:
            try:
                truck = FleetTruck.objects.get(id=truck_id)
            except:
                pass
        mission = FleetMission.objects.filter(driver=driver, status__in=['enroute', 'in_progress']).first() if driver else None
        driver_name = driver.get_display_name() if driver else 'Unknown Driver'
        truck_name = truck.truck_identifier if truck else 'Unknown Truck'
        msg = f'📢 DRIVER ({driver_name}/{truck_name}): {message}'
        alert = Alert.objects.create(id=uuid.uuid4(), truck=truck, driver=driver, mission=mission, alert_type='driver_alert', severity='high', message=msg, location_lat=latitude, location_lon=longitude, speed_kmh=speed)
        log_alert_to_activity(truck, driver, mission, 'driver_alert', msg, latitude, longitude, speed, 'high')
        logger.info(f'📢 Driver alert from {driver_name}: {message}')
        return JsonResponse({'success': True, 'alert_id': str(alert.id), 'message': 'Alert sent to fleet manager', 'alert': {'id': str(alert.id), 'type': alert.alert_type, 'severity': alert.severity, 'message': alert.message, 'created_at': alert.created_at.isoformat()}}, status=201)
    except Exception as e:
        logger.error(f'Driver alert error: {str(e)}')
        return JsonResponse({'error': str(e)}, status=400)

@require_http_methods(['POST'])
@csrf_exempt
def apply_production_indexes(request):
    """
    POST /api/v1/production/apply-indexes/

    Creates database indexes for production-scale performance.
    Safe to run multiple times - uses IF NOT EXISTS / CREATE INDEX CONCURRENTLY

    Indexes created:
    1. fleet_truck_locations (truck_id, timestamp) - for trail queries
    2. fleet_truck_locations (timestamp) - for time-range queries
    3. fleet_activities (truck_id, activity_type, timestamp) - for audit trail
    4. fleet_missions (truck_id, status) - for mission lookups
    5. fleet_missions (driver_id, status) - for driver mission lookups
    """
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                indexes = ['\n                    CREATE INDEX CONCURRENTLY IF NOT EXISTS \n                        idx_truck_locations_truck_ts \n                    ON fleet_truck_locations (truck_id, timestamp DESC)\n                    ', '\n                    CREATE INDEX CONCURRENTLY IF NOT EXISTS \n                        idx_truck_locations_ts \n                    ON fleet_truck_locations (timestamp DESC)\n                    ', '\n                    CREATE INDEX CONCURRENTLY IF NOT EXISTS \n                        idx_activities_truck_type_ts \n                    ON fleet_activities (truck_id, activity_type, timestamp DESC)\n                    ', '\n                    CREATE INDEX CONCURRENTLY IF NOT EXISTS \n                        idx_missions_truck_status \n                    ON fleet_missions (truck_id, status)\n                    ', '\n                    CREATE INDEX CONCURRENTLY IF NOT EXISTS \n                        idx_missions_driver_status \n                    ON fleet_missions (driver_id, status)\n                    ', '\n                    CREATE INDEX CONCURRENTLY IF NOT EXISTS \n                        idx_activities_category_ts \n                    ON fleet_activities (activity_category, timestamp DESC)\n                    ']
                results = []
                for sql in indexes:
                    try:
                        cursor.execute(sql)
                        results.append({'sql': sql[:80] + '...', 'status': 'created'})
                    except Exception as e:
                        try:
                            non_current = sql.replace('CONCURRENTLY ', '')
                            cursor.execute(non_current)
                            results.append({'sql': sql[:80] + '...', 'status': 'created (fallback)'})
                        except Exception as e2:
                            results.append({'sql': sql[:80] + '...', 'status': f'failed: {str(e2)[:50]}'})
                try:
                    cursor.execute('\n                        CREATE INDEX CONCURRENTLY IF NOT EXISTS \n                            idx_truck_locations_lookup \n                        ON fleet_truck_locations (truck_id, timestamp DESC, latitude, longitude, speed)\n                    ')
                    results.append({'sql': 'idx_truck_locations_lookup', 'status': 'created'})
                except Exception:
                    try:
                        cursor.execute('\n                            CREATE INDEX IF NOT EXISTS \n                                idx_truck_locations_lookup \n                            ON fleet_truck_locations (truck_id, timestamp DESC, latitude, longitude, speed)\n                        ')
                        results.append({'sql': 'idx_truck_locations_lookup', 'status': 'created (fallback)'})
                    except Exception as e:
                        err_msg = str(e)[:50] if e else 'unknown error'
                        results.append({'sql': 'idx_truck_locations_lookup', 'status': f'skipped: {err_msg}'})
        return JsonResponse({'status': 'success', 'message': 'Production indexes applied', 'results': results}, status=200)
    except Exception as e:
        logger.error(f'Index creation error: {str(e)}')
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

@require_http_methods(['POST'])
@csrf_exempt
def cleanup_old_locations(request):
    """
    POST /api/v1/production/cleanup-locations/

    Deletes old TruckLocation records older than N days to save storage.
    Only keeps the most recent 1000 points per truck to maintain trail quality.

    Request body: { "days": 90, "max_points_per_truck": 1000 }
    """
    try:
        data = json.loads(request.body) if request.body else {}
        days = int(data.get('days', 90))
        max_points = int(data.get('max_points_per_truck', 1000))
        cutoff = timezone.now() - timedelta(days=days)
        from .models import TruckLocation, FleetTruck
        total_before = TruckLocation.objects.count()
        deleted_batch, _ = TruckLocation.objects.filter(timestamp__lt=cutoff).delete()
        trucks = FleetTruck.objects.all()
        trimmed_count = 0
        for truck in trucks:
            keep_ids = TruckLocation.objects.filter(truck=truck).order_by('-timestamp').values_list('id', flat=True)[:max_points]
            deleted, _ = TruckLocation.objects.filter(truck=truck).exclude(id__in=list(keep_ids)).delete()
            trimmed_count += deleted
        after = TruckLocation.objects.count()
        from .models import FleetActivity
        audit_deleted, _ = FleetActivity.objects.filter(timestamp__lt=cutoff, activity_category='trail').delete()
        return JsonResponse({'status': 'success', 'records_before': total_before, 'records_after': after, 'old_records_deleted': int(deleted_batch or 0), 'trimmed_records': int(trimmed_count), 'audit_entries_deleted': int(audit_deleted or 0), 'retention_days': days, 'max_points_per_truck': max_points}, status=200)
    except Exception as e:
        logger.error(f'Cleanup error: {str(e)}')
        return JsonResponse({'error': str(e)}, status=500)

