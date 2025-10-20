#!/bin/bash

# =============================================================================
# PostgreSQL + PostGIS + pgRouting Setup Script
# =============================================================================
# This script automates the complete setup of a routing database with:
# - PostgreSQL 17
# - PostGIS extension
# - pgRouting extension
# - OSM data processing
# - Database configuration
# =============================================================================

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration variables
DB_NAME="kitchener_routing"
DB_USER="postgres"
DB_PASSWORD="takeme@kw"
OSM_FILE="ontario-latest.osm.pbf"
REGION_FILE="kitchener.osm.pbf"
REGION_OSM="kitchener.osm"
BOUNDING_BOX="-80.6639970464,43.3480624398,-80.3339821224,43.5571680888" 

# Used a bounding box to only get the Kitchener region. you can get your own bounding box from https://boundingbox.klokantech.com/
# change the DB_PASSWORD to your own password.
# change the DB_NAME to your own database name.
# change the OSM_FILE to your own OSM file.
# change the REGION_FILE to your own region file.
# change the REGION_OSM to your own region OSM file.
# change the BOUNDING_BOX to your own bounding box.

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to wait for PostgreSQL to be ready
wait_for_postgres() {
    print_status "Waiting for PostgreSQL to be ready..."
    while ! sudo -u postgres psql -c "SELECT 1;" >/dev/null 2>&1; do
        sleep 2
    done
    print_success "PostgreSQL is ready"
}

# Main setup function
main() {
    print_status "Starting PostgreSQL + PostGIS + pgRouting setup..."
    print_status "This script will install and configure everything needed for routing"
    
    # Update system packages
    print_status "Updating system packages..."
    sudo apt update -y
    sudo apt upgrade -y
    
    # Step 1: Install required packages
    print_status "Installing PostgreSQL, PostGIS, and routing tools..."
    sudo apt install -y \
        postgresql \
        postgresql-contrib \
        postgis \
        osm2pgrouting \
        osm2pgsql \
        wget \
        unzip \
        osmctools \
        postgresql-17-pgrouting
    
    print_success "All packages installed successfully"
    
    # Step 2: Download OSM data (if not already present)
    if [ ! -f "$OSM_FILE" ]; then
        print_status "Downloading Ontario OSM data..."
        wget https://download.geofabrik.de/north-america/canada/ontario-latest.osm.pbf
        print_success "OSM data downloaded"
    else
        print_warning "OSM file already exists, skipping download"
    fi
    
    # Step 3: Convert OSM data to region-specific file
    print_status "Converting OSM data to Kitchener region..."
    osmconvert "$OSM_FILE" \
        -b="$BOUNDING_BOX" \
        --complete-ways \
        --complete-multipolygons \
        --complete-boundaries \
        -o="$REGION_FILE"
    print_success "Region-specific OSM data created"
    
    # Step 4: Convert to OSM format
    print_status "Converting to OSM format..."
    osmconvert "$REGION_FILE" -o="$REGION_OSM"
    print_success "OSM format conversion completed"
    
    # Step 5: Configure PostgreSQL
    print_status "Configuring PostgreSQL..."
    
    # Set postgres password
    sudo -u postgres psql -c "ALTER USER postgres WITH PASSWORD '$DB_PASSWORD';"
    
    # Restart PostgreSQL
    sudo systemctl restart postgresql
    sudo systemctl enable postgresql
    
    # Wait for PostgreSQL to be ready
    wait_for_postgres
    
    # Step 6: Create database and install extensions
    print_status "Creating database and installing extensions..."
    sudo -u postgres psql -c "CREATE DATABASE $DB_NAME;" || print_warning "Database might already exist"
    sudo -u postgres psql -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS postgis;"
    sudo -u postgres psql -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS pgrouting;"
    print_success "Database and extensions configured"
    
    # Step 7: Import OSM data using osm2pgrouting
    print_status "Importing OSM data into database (this may take several minutes)..."
    osm2pgrouting \
        -f "$REGION_OSM" \
        -d "$DB_NAME" \
        -U "$DB_USER" \
        -h localhost \
        -W "$DB_PASSWORD" \
        --clean \
        -c /usr/share/osm2pgrouting/mapconfig_for_cars.xml
    
    print_success "OSM data import completed"
    
    # Step 8: Create additional indexes for better performance
    print_status "Creating performance indexes..."
    sudo -u postgres psql -d "$DB_NAME" -c "
        CREATE INDEX IF NOT EXISTS idx_ways_source ON ways(source);
        CREATE INDEX IF NOT EXISTS idx_ways_target ON ways(target);
        CREATE INDEX IF NOT EXISTS idx_ways_vertices_pgr_geom ON ways_vertices_pgr USING GIST(the_geom);
        CREATE INDEX IF NOT EXISTS idx_ways_geom ON ways USING GIST(the_geom);
        ANALYZE ways;
        ANALYZE ways_vertices_pgr;
    "
    print_success "Performance indexes created"
    
    # Step 8.5: Add time-based cost columns and calculate values
    print_status "Adding time-based cost columns to ways table..."
    sudo -u postgres psql -d "$DB_NAME" -c "
        ALTER TABLE ways ADD COLUMN IF NOT EXISTS cost_time DOUBLE PRECISION;
        ALTER TABLE ways ADD COLUMN IF NOT EXISTS reverse_cost_time DOUBLE PRECISION;
        
        UPDATE ways
        SET cost_time = CASE
            WHEN maxspeed_forward > 0 THEN length_m / (maxspeed_forward * 1000 / 3600)
            ELSE length_m / (50 * 1000 / 3600) -- fallback 50 km/h
        END,
        reverse_cost_time = CASE
            WHEN maxspeed_backward > 0 THEN length_m / (maxspeed_backward * 1000 / 3600)
            ELSE length_m / (50 * 1000 / 3600)
        END;
    "
    print_success "Time-based cost columns added and calculated"
    
    # Step 9: Test the setup
    print_status "Testing the setup..."
    sudo -u postgres psql -d "$DB_NAME" -c "
        SELECT 
            'Database: ' || current_database() as info
        UNION ALL
        SELECT 'PostGIS: ' || PostGIS_version()
        UNION ALL
        SELECT 'pgRouting: ' || pgr_version()
        UNION ALL
        SELECT 'Ways count: ' || count(*)::text FROM ways
        UNION ALL
        SELECT 'Vertices count: ' || count(*)::text FROM ways_vertices_pgr;
    "
    
    print_success "Setup completed successfully!"
    print_status "Database connection details:"
    echo "  Host: localhost"
    echo "  Port: 5432"
    echo "  Database: $DB_NAME"
    echo "  User: $DB_USER"
    echo "  Password: $DB_PASSWORD"
    
    print_status "You can now use your routing API with these connection details"
}

# Error handling
trap 'print_error "Setup failed at line $LINENO"' ERR

# Run main function
main "$@"

# for this its required 8gb ram for this OSM data for kitchener region. you can also run smaller region with 4gb ram. 
 
# Optional for VM installation, start your tunnel to the database from your local machine. 
# ssh -L 5432:localhost:5432 root@<your_vm_ip>

# connect DB with QGIS and Visualize the map data. also run sql to see routes.


# Alter table for adding a timed weighs, and then insert the data.

