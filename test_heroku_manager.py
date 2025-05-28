#!/usr/bin/env python
"""
Test script for the heroku_manager package.
This script verifies that the package can be imported and used correctly.
"""

import os
import sys
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Add the parent directory to the Python path so we can import the package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    # Import the package
    from heroku_manager import HerokuManager, HerokuDyno, get_dyno_settings, DYNO_SIZES, WORKER_SETTINGS_MAP
    logger.info("Successfully imported heroku_manager package")

    # Test get_dyno_settings function
    settings = get_dyno_settings()
    logger.info(f"Default dyno settings: {settings}")

    # Test DYNO_SIZES constant
    logger.info(f"Available dyno sizes: {list(DYNO_SIZES.keys())}")

    # Test WORKER_SETTINGS_MAP constant
    logger.info(f"Worker settings map keys: {list(WORKER_SETTINGS_MAP.keys())}")

    # Test HerokuManager singleton
    if not os.environ.get('HEROKU_API_KEY'):
        logger.warning("HEROKU_API_KEY not set, skipping HerokuManager tests")
    else:
        # Get the autoscaler instance
        autoscaler = HerokuManager.get_autoscaler()
        logger.info(f"Got autoscaler instance: {autoscaler}")

        # Test that it's a singleton
        autoscaler2 = HerokuManager.get_autoscaler()
        logger.info(f"Singleton test passed: {autoscaler is autoscaler2}")

    logger.info("All tests passed!")

except Exception as e:
    logger.error(f"Error testing heroku_manager package: {e}", exc_info=True)
    sys.exit(1)
