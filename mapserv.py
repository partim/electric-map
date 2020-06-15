# This file is based on osgende-mapserv-falcon.py from osgende.
#
# For more information see https://github.com/waymarkedtrails/osgende
# 
# Copyright (C) 2020 Sarah Hoffmann
#
# This is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
"""
Falcon-based tile server.

Use with an WSGI server. For uwsgi, this may or may not be the correct
incantation:

    uwsgi --http-socket :8088 --plugin python3 --file basemap/mapserv.py --enable-threads

When tweaking the map, you might want to:

    uwsgi --http-socket :8088 --plugin python3 --file basemap/mapserv.py --enable-threads --touch-reload basemap/basemap.xml
"""

import datetime
import os
import sys
import threading
import hashlib
from math import pi,exp,atan

import falcon
import mapnik
import psycopg2

RAD_TO_DEG = 180/pi

class TileProjection:
    def __init__(self,levels=18):
        self.Bc = []
        self.Cc = []
        self.zc = []
        self.Ac = []
        c = 256
        for d in range(0,levels + 1):
            e = c/2;
            self.Bc.append(c/360.0)
            self.Cc.append(c/(2 * pi))
            self.zc.append((e,e))
            self.Ac.append(c)
            c *= 2

    def tile_nw(self, zoom, x, y):
         e = self.zc[zoom]
         f = (x*256.0 - e[0])/self.Bc[zoom]
         g = (y*256.0 - e[1])/-self.Cc[zoom]
         h = RAD_TO_DEG * ( 2 * atan(exp(g)) - 0.5 * pi)
         return (f,h)


class MapnikRenderer(object):
    def __init__(self, style):
        self.formats = ('png',)
        self.tile_size = (512, 512)
        self.max_zoom = 15
        self.style = style

        m = mapnik.Map(*self.tile_size)
        mapnik.load_map(m, self.style)

        self.mproj = mapnik.Projection(m.srs)
        self.gproj = TileProjection(self.max_zoom)
        self.thread_data = threading.local()

    def get_map(self):
        self.thread_map()
        return self.thread_data.map

    def thread_map(self):
        if not hasattr(self.thread_data, 'map'):
            m = mapnik.Map(*self.tile_size)
            mapnik.load_map(m, self.style)
            self.thread_data.map = m

    def split_url(self, zoom, x, y):
        ypt = y.find('.')
        if ypt < 0:
            return None
        tiletype = y[ypt+1:]
        if tiletype not in self.formats:
            return None
        try:
            zoom = int(zoom)
            x = int(x)
            y = int(y[:ypt])
        except ValueError:
            return None

        if zoom > self.max_zoom:
            return None

        return (zoom, x, y, tiletype)

    def render(self, zoom, x, y, fmt):
        p0 = self.gproj.tile_nw(zoom, x, y+1)
        p1 = self.gproj.tile_nw(zoom, x+1, y)

        c0 = self.mproj.forward(mapnik.Coord(p0[0],p0[1]))
        c1 = self.mproj.forward(mapnik.Coord(p1[0],p1[1]))

        bbox = mapnik.Box2d(c0.x, c0.y, c1.x, c1.y)
        im = mapnik.Image(*self.tile_size)

        m = self.get_map()
        m.zoom_to_box(bbox)
        mapnik.render(m, im)

        return im.tostring('png256')

    def on_get(self, req, resp, zoom, x, y):
        tile_desc = self.split_url(zoom, x, y)
        if tile_desc is None:
            raise falcon.HTTPNotFound()

        tile = self.render(*tile_desc)

        resp.content_type = "image/png"
        resp.body = tile


class VectorRenderer(object):
    def __init__(self):
        self.max_zoom = 15

        self.gproj = TileProjection(self.max_zoom)
        self.thread_data = threading.local()

    def get_db(self):
        if not hasattr(self.thread_data, 'db'):
            self.thread_data.db = psycopg2.connect(dbname="osm")
        return self.thread_data.db

    def render(self, zoom, x, y):
        p0 = self.gproj.tile_nw(zoom, x, y+1)
        p1 = self.gproj.tile_nw(zoom, x+1, y)

        cursor = self.get_db().cursor()
        cursor.execute("""\
            SELECT ST_AsMVT(tile) FROM (
                SELECT
                    id, name, railway, voltage, frequency,
                    ST_AsMVTGeom(
                        way,
                        ST_Makebox2d(
                            ST_transform(
                                ST_SetSrid(ST_MakePoint(%s, %s), 4326), 3857
                            ),
                            ST_transform(
                                ST_SetSrid(ST_MakePoint(%s,%s), 4326), 3857
                            )
                        ),
                        4096, 0, false
                    ) AS geom
                FROM osm_line
            ) AS tile
            """,
            (p0[0], p0[1], p1[0], p1[1])
        )
        return str(cursor.fetchone()[0])

    def on_get(self, req, resp, zoom, x, y):
        try:
            zoom = int(zoom)
            x = int(x)
            y = int(y)
        except ValueError:
            raise falcon.HTTPNotFound()

        tile = self.render(zoom, x, y)

        resp.content_type = "application/octet-stream"
        resp.body = tile


class TestMap(object):

    DEFAULT_TESTMAP="""\
        <!DOCTYPE html>
        <html>
        <head>
            <title>Testmap</title>
            <link
                rel="stylesheet"
                href="https://unpkg.com/leaflet@1.6.0/dist/leaflet.css"
            />
        </head>
        <body >
            <div id="map" style="position: absolute; width: 99%; height: 97%">
            </div>

            <script src="https://unpkg.com/leaflet@1.6.0/dist/leaflet.js">
            </script>
            <script>
                var map = L.map('map').setView([52.2384, 7.0580], 14);
                L.tileLayer('/vector/{z}/{x}/{y}', {
                    maxZoom: 15,
                }).addTo(map);
            </script>
        </body>
        </html>
        """

    def on_get(self, req, resp):
        resp.content_type = "text/html"
        resp.body = self.DEFAULT_TESTMAP


def setup(app):
    basepath = os.path.dirname(__file__);
    app.add_route('/', TestMap())
    app.add_route(
        '/raster/{zoom}/{x}/{y}',
        MapnikRenderer(os.path.join(basepath, "basemap.xml"))
    )
    app.add_route(
        '/vector/{zoom}/{x}/{y}',
        VectorRenderer(os.path.join(basepath, "basemap.xml"))
    )


application = falcon.API()
setup(application)

