from flask import Blueprint, request, make_response
# from flask import current_app
# from google.cloud import pubsub_v1
import json
# import numpy as np
# import time
# import datetime
# import sys
# import os
# import traceback

from pychunkedgraph.meshing import meshgen
from pychunkedgraph.app import app_utils


bp = Blueprint('pychunkedgraph_meshing', __name__, url_prefix="/meshing")

# -------------------------------
# ------ Access control and index
# -------------------------------

@bp.route('/')
@bp.route("/index")
def index():
    return "Manifest Server -- 0.1"


@bp.route
def home():
    resp = make_response()
    resp.headers['Access-Control-Allow-Origin'] = '*'
    acah = "Origin, X-Requested-With, Content-Type, Accept"
    resp.headers["Access-Control-Allow-Headers"] = acah
    resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    resp.headers["Connection"] = "keep-alive"
    return resp


# ------------------------------------------------------------------------------


@bp.route('/1.0/mesh_lvl', method=['POST'])
def mesh_(node_id):
    data = json.loads(request.data)

    if "sv_ids" in data:
        sv_ids = data["sv_ids"]
    else:
        sv_ids = None

    cg = app_utils.get_cg()

    meshgen.mesh_lvl2_preview(cg, node_id, mip=2, supervoxel_ids=sv_ids)