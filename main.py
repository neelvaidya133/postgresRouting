from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import psycopg2
import json
from dotenv import load_dotenv
import os

app = FastAPI()
load_dotenv()

# Pydantic models for request/response
class CoordinateRequest(BaseModel):
    start_coords: str  # Format: "lat,lng" e.g., "43.43656,-80.45172"
    end_coords: str    # Format: "lat,lng" e.g., "43.43656,-80.45172"

class RouteFromBatchRequest(BaseModel):
    geocode_results: list[dict]

class RouteFromBatchResponse(BaseModel):
    success: bool
    total_stops: int
    route_geojson: dict
    stops: list[dict]

def get_connection():
    return psycopg2.connect(
        dbname="kitchener_routing",
        user="postgres",
        password="takeme@kw",
        host="localhost",
        port="5432"
    )

def create_multi_stop_route(geocode_results: list[dict]) -> dict:
    """Create a multi-stop route using pgRouting from geocoding results"""
    try:
        # Filter successful geocodes and sort by stop_number
        successful_stops = [stop for stop in geocode_results if stop.get("status") == "success"]
        successful_stops.sort(key=lambda x: x.get("stop_number", 0))
        
        if len(successful_stops) < 2:
            return {
                "success": False,
                "error": "At least 2 successful geocodes required for routing",
                "successful_stops": len(successful_stops)
            }
        
        if len(successful_stops) > 100:
            return {
                "success": False,
                "error": "Maximum 100 stops allowed",
                "successful_stops": len(successful_stops)
            }
        
        # Build the stops CTE dynamically
        stops_cte = "WITH stops AS (\n"
        for i, stop in enumerate(successful_stops, 1):
            stops_cte += f"  SELECT {i} AS id, ST_SetSRID(ST_Point({stop['lng']},{stop['lat']}),4326) AS geom"
            if i < len(successful_stops):
                stops_cte += " UNION ALL\n"
            else:
                stops_cte += "\n"
        stops_cte += "),\n"
        
        # Build the complete query
        query = f"""
        {stops_cte}
        snap AS (
          SELECT id, (
            SELECT id FROM ways_vertices_pgr
            ORDER BY the_geom <-> stops.geom
            LIMIT 1
          ) AS vertex_id
          FROM stops
        ),
        pairs AS (
          SELECT a.id AS from_id, a.vertex_id AS source,
                 b.id AS to_id,   b.vertex_id AS target
          FROM snap a
          JOIN snap b ON b.id = a.id + 1
        ),
        route_parts AS (
          SELECT p.from_id, p.to_id, r.seq, r.node, r.edge, w.the_geom
          FROM pairs p,
               pgr_astar(
                 'SELECT gid AS id, source, target,
                         cost_time AS cost,
                         reverse_cost_time AS reverse_cost,
                         x1, y1, x2, y2
                  FROM ways
                  WHERE true',
                 p.source, p.target,
                 directed := true
               ) AS r
          LEFT JOIN ways w ON r.edge = w.gid
        ),
        full_route AS (
          SELECT ST_LineMerge(ST_Collect(the_geom))::geometry(MultiLineString,4326) AS geom
          FROM route_parts
        )
        SELECT ST_AsGeoJSON(geom) AS route_geojson FROM full_route;
        """
        
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(query)
        result = cur.fetchone()
        cur.close()
        conn.close()
        
        if result and result[0]:
            # Prepare stop details for response
            stop_details = []
            for stop in successful_stops:
                stop_info = {
                    "stop_number": stop.get("stop_number", 0),
                    "address": stop.get("address", ""),
                    "lat": stop.get("lat"),
                    "lng": stop.get("lng"),
                    "formatted_address": stop.get("formatted_address", "")
                }
                stop_details.append(stop_info)
            
            return {
                "success": True,
                "route_geojson": json.loads(result[0]),
                "total_stops": len(successful_stops),
                "stops": stop_details
            }
        else:
            return {
                "success": False,
                "error": "No route found"
            }
            
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@app.post("/route")
def get_route(request: CoordinateRequest):
    """
    Calculate route between two points using coordinate strings.
    
    Request body:
    {
        "start_coords": "43.43656,-80.45172",
        "end_coords": "43.43656,-80.45172"
    }
    """
    try:
        # Parse start coordinates
        start_lat, start_lng = request.start_coords.split(',')
        start_lat = float(start_lat.strip())
        start_lng = float(start_lng.strip())
        
        # Parse end coordinates
        end_lat, end_lng = request.end_coords.split(',')
        end_lat = float(end_lat.strip())
        end_lng = float(end_lng.strip())
        
        query = f"""
        WITH stops AS (
          SELECT 1 AS id, ST_SetSRID(ST_Point({start_lng},{start_lat}),4326) AS geom UNION ALL
          SELECT 2, ST_SetSRID(ST_Point({end_lng},{end_lat}),4326)
        ),
        snap AS (
          SELECT id, (
            SELECT id FROM ways_vertices_pgr
            ORDER BY the_geom <-> stops.geom
            LIMIT 1
          ) AS vertex_id
          FROM stops
        ),
        route_parts AS (
          SELECT r.seq, r.node, r.edge, w.the_geom
          FROM pgr_astar(
            'SELECT gid AS id, source, target, cost_time AS cost, reverse_cost_time AS reverse_cost, x1, y1, x2, y2 FROM ways',
            (SELECT vertex_id FROM snap WHERE id=1),
            (SELECT vertex_id FROM snap WHERE id=2),
            directed := true
          ) AS r
          LEFT JOIN ways w ON r.edge = w.gid
        ),
        full_route AS (
          SELECT ST_LineMerge(ST_Collect(the_geom))::geometry(MultiLineString,4326) AS geom
          FROM route_parts
        )
        SELECT ST_AsGeoJSON(geom) AS route_geojson FROM full_route;
        """
        
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(query)
        result = cur.fetchone()
        cur.close()
        conn.close()
        
        if result and result[0]:
            return {
                "success": True,
                "route_geojson": json.loads(result[0]),
                "start_coords": request.start_coords,
                "end_coords": request.end_coords
            }
        else:
            return {"error": "No route found"}
    except ValueError as e:
        return {"error": f"Invalid coordinate format. Expected 'lat,lng' format. Error: {str(e)}"}
    except Exception as e:
        return {"error": str(e)}

@app.post("/route-from-batch", response_model=RouteFromBatchResponse)
def route_from_batch_geocoding(request: RouteFromBatchRequest):
    """
    Create a multi-stop route from batch geocoding results.
    Takes the results from /geocode-batch and creates a route.
    
    Request body:
    {
        "geocode_results": [
            {
                "stop_number": 1,
                "address": "Stop 1: 85 Duke St, Kitchener, ON",
                "lat": 43.451715,
                "lng": -80.4913,
                "formatted_address": "85 Duke Street West, Kitchener, Ontario N2H 0B7, Canada",
                "confidence": 0,
                "status": "success"
            },
            {
                "stop_number": 2,
                "address": "Stop 2: 155 King St, Kitchener, ON",
                "lat": 43.450672,
                "lng": -80.492076,
                "formatted_address": "155 King Street West, Kitchener, Ontario N2G 1A7, Canada",
                "confidence": 0,
                "status": "success"
            }
        ]
    }
    
    Response:
    {
        "success": true,
        "total_stops": 2,
        "route_geojson": {
            "type": "MultiLineString",
            "coordinates": [...]
        },
        "stops": [
            {
                "stop_number": 1,
                "address": "Stop 1: 85 Duke St, Kitchener, ON",
                "lat": 43.451715,
                "lng": -80.4913,
                "formatted_address": "85 Duke Street West, Kitchener, Ontario N2H 0B7, Canada"
            }
        ]
    }
    """
    try:
        if len(request.geocode_results) < 2:
            raise HTTPException(status_code=400, detail="At least 2 stops required for routing")
        
        if len(request.geocode_results) > 100:
            raise HTTPException(status_code=400, detail="Maximum 100 stops allowed")
        
        # Convert Pydantic models to dicts for processing
        geocode_dicts = [item for item in request.geocode_results]
        
        # Create the multi-stop route
        route_result = create_multi_stop_route(geocode_dicts)
        
        if not route_result["success"]:
            raise HTTPException(status_code=400, detail=route_result["error"])
        
        return RouteFromBatchResponse(**route_result)
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Route creation failed: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
