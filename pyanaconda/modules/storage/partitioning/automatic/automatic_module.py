#
# Auto partitioning module.
#
# Copyright (C) 2018 Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#
import copy

from blivet.devices import PartitionDevice
from blivet.size import Size

from pyanaconda.anaconda_loggers import get_module_logger
from pyanaconda.core.constants import DEFAULT_AUTOPART_TYPE
from pyanaconda.core.dbus import DBus
from pyanaconda.core.signal import Signal
from pyanaconda.modules.common.constants.objects import AUTO_PARTITIONING
from pyanaconda.modules.common.errors.storage import UnknownDeviceError, ProtectedDeviceError
from pyanaconda.modules.common.structures.partitioning import PartitioningRequest
from pyanaconda.modules.storage.partitioning.base import PartitioningModule
from pyanaconda.modules.storage.partitioning.automatic.automatic_interface import \
    AutoPartitioningInterface
from pyanaconda.modules.storage.partitioning.constants import PartitioningMethod
from pyanaconda.modules.storage.partitioning.automatic.automatic_partitioning import \
    AutomaticPartitioningTask

log = get_module_logger(__name__)


class AutoPartitioningModule(PartitioningModule):
    """The auto partitioning module."""

    def __init__(self):
        """Initialize the module."""
        super().__init__()
        self.enabled_changed = Signal()
        self._enabled = False

        self.request_changed = Signal()
        self._request = PartitioningRequest()

    @property
    def partitioning_method(self):
        """Type of the partitioning method."""
        return PartitioningMethod.AUTOMATIC

    def for_publication(self):
        """Return a DBus representation."""
        return AutoPartitioningInterface(self)

    def publish(self):
        """Publish the module."""
        DBus.publish_object(AUTO_PARTITIONING.object_path, self.for_publication())

    def process_kickstart(self, data):
        """Process the kickstart data."""
        self.set_enabled(data.autopart.autopart)
        request = PartitioningRequest()

        if data.autopart.type is not None:
            request.partitioning_scheme = data.autopart.type

        if data.autopart.fstype:
            request.file_system_type = data.autopart.fstype

        if data.autopart.noboot:
            request.excluded_mount_points.append("/boot")

        if data.autopart.nohome:
            request.excluded_mount_points.append("/home")

        if data.autopart.noswap:
            request.excluded_mount_points.append("swap")

        if data.autopart.encrypted:
            request.encrypted = True
            request.passphrase = data.autopart.passphrase
            request.cipher = data.autopart.cipher
            request.luks_version = data.autopart.luks_version

            request.pbkdf = data.autopart.pbkdf
            request.pbkdf_memory = data.autopart.pbkdf_memory
            request.pbkdf_time = data.autopart.pbkdf_time
            request.pbkdf_iterations = data.autopart.pbkdf_iterations

            request.escrow_certificate = data.autopart.escrowcert
            request.backup_passphrase_enabled = data.autopart.backuppassphrase

        self.set_request(request)

    def setup_kickstart(self, data):
        """Setup the kickstart data."""
        data.autopart.autopart = self.enabled
        data.autopart.fstype = self.request.file_system_type

        if self.request.partitioning_scheme != DEFAULT_AUTOPART_TYPE:
            data.autopart.type = self.request.partitioning_scheme

        data.autopart.nohome = "/home" in self.request.excluded_mount_points
        data.autopart.noboot = "/boot" in self.request.excluded_mount_points
        data.autopart.noswap = "swap" in self.request.excluded_mount_points

        data.autopart.encrypted = self.request.encrypted

        # Don't generate sensitive information.
        data.autopart.passphrase = ""
        data.autopart.cipher = self.request.cipher
        data.autopart.luks_version = self.request.luks_version

        data.autopart.pbkdf = self.request.pbkdf
        data.autopart.pbkdf_memory = self.request.pbkdf_memory
        data.autopart.pbkdf_time = self.request.pbkdf_time
        data.autopart.pbkdf_iterations = self.request.pbkdf_iterations

        data.autopart.escrowcert = self.request.escrow_certificate
        data.autopart.backuppassphrase = self.request.backup_passphrase_enabled

    @property
    def enabled(self):
        """Is the auto partitioning enabled?"""
        return self._enabled

    def set_enabled(self, enabled):
        """Is the auto partitioning enabled?

        :param enabled: a boolean value
        """
        self._enabled = enabled
        self.enabled_changed.emit()
        log.debug("Enabled is set to '%s'.", enabled)

    @property
    def request(self):
        """The partitioning request."""
        return self._request

    def set_request(self, request):
        """Set the partitioning request.

        :param request: a request
        """
        self._request = request
        self.request_changed.emit()
        log.debug("Request is set to '%s'.", request)

    def requires_passphrase(self):
        """Is the default passphrase required?

        :return: True or False
        """
        return self.request.encrypted and not self.request.passphrase

    def set_passphrase(self, passphrase):
        """Set a default passphrase for all encrypted devices.

        :param passphrase: a string with a passphrase
        """
        # Update the request with a new copy.
        request = copy.deepcopy(self.request)
        request.passphrase = passphrase
        self.set_request(request)

    def _get_device(self, name):
        """Find a device by its name.

        :param name: a name of the device
        :return: an instance of the Blivet's device
        :raise: UnknownDeviceError if no device is found
        """
        device = self.storage.devicetree.get_device_by_name(name, hidden=True)

        if not device:
            raise UnknownDeviceError(name)

        return device

    def remove_device(self, device_name):
        """Remove a device after removing its dependent devices.

        If the device is protected, do nothing. If the device has
        protected children, just remove the unprotected ones.

        :param device_name: a name of the device
        """
        device = self._get_device(device_name)

        if device.protected:
            raise ProtectedDeviceError(device_name)

        # Only remove unprotected children if any protected.
        if any(d.protected for d in device.children):
            log.debug("Removing unprotected children of %s.", device_name)

            for child in (d for d in device.children if not d.protected):
                self.storage.recursive_remove(child)

            return

        # No protected children, remove the device
        log.debug("Removing device %s.", device_name)
        self.storage.recursive_remove(device)

    def shrink_device(self, device_name, size):
        """Shrink the size of the device.

        :param device_name: a name of the device
        :param size: a new size in bytes
        """
        size = Size(size)
        device = self._get_device(device_name)

        if device.protected:
            raise ProtectedDeviceError(device_name)

        # The device size is small enough.
        if device.size <= size:
            log.debug("The size of %s is already %s.", device_name, device.size)
            return

        # Resize the device.
        log.debug("Shrinking a size of %s to %s.", device_name, size)
        aligned_size = device.align_target_size(size)
        self.storage.resize_device(device, aligned_size)

    def is_device_partitioned(self, device_name):
        """Is the specified device partitioned?

        :param device_name: a name of the device
        :return: True or False
        """
        device = self._get_device(device_name)
        return self._is_device_partitioned(device)

    def _is_device_partitioned(self, device):
        """Is the specified device partitioned?"""
        return device.is_disk and device.partitioned and device.format.supported

    def get_device_partitions(self, device_name):
        """Get partitions of the specified device.

        :param device_name: a name of the device
        :return: a list of device names
        """
        device = self._get_device(device_name)

        if not self._is_device_partitioned(device):
            return []

        return [
            d.name for d in device.children
            if isinstance(d, PartitionDevice)
            and not (d.is_extended and d.format.logical_partitions)
        ]

    def is_device_resizable(self, device_name):
        """Is the specified device resizable?

        :param device_name: a name of the device
        :return: True or False
        """
        device = self._get_device(device_name)
        return device.resizable

    def get_device_size_limits(self, device_name):
        """Get size limits of the given device.

        :param device_name: a name of the device
        :return: a tuple of min and max sizes in bytes
        """
        device = self._get_device(device_name)
        return device.min_size.get_bytes(), device.max_size.get_bytes()

    def configure_with_task(self):
        """Schedule the partitioning actions."""
        return AutomaticPartitioningTask(self.storage, self.request)