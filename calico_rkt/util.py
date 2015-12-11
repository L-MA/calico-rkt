# Copyright 2015 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import logging
from constants import LOG_DIR, LOG_FILENAME


def configure_logging(logger, log_filename=LOG_FILENAME, log_dir=LOG_DIR,
                      log_level=logging.DEBUG):
    """Configures logging for given logger using the given filename.

    :return None.
    """
    # If the logging directory doesn't exist, create it.
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    log_path = os.path.join(log_dir, log_filename)

    # Create a log handler and formtter and apply to _log.
    hdlr = logging.FileHandler(filename=log_path)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    hdlr.setFormatter(formatter)
    logger.addHandler(hdlr)

    # Set the log level.
    logger.setLevel(log_level)