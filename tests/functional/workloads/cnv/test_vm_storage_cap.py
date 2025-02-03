import logging
import random

import pytest

from ocs_ci.framework.pytest_customization.marks import magenta_squad, workloads
from ocs_ci.framework.testlib import E2ETest
from ocs_ci.helpers.cnv_helpers import (
    run_dd_io,
    all_critical_pods_running,
    all_nodes_ready,
)
from ocs_ci.ocs import constants
from ocs_ci.ocs.resources.pod import get_pod_restarts_count, get_ceph_tools_pod
from ocs_ci.ocs.resources.storage_cluster import add_capacity

logger = logging.getLogger(__name__)


@magenta_squad
@workloads
@pytest.mark.polarion_id("OCS-")
class TestVmStorageCapacity(E2ETest):
    """
    Perform Add capacity operation while the VMs are in
     different states and in the presence of snapshots and clones of the VMs
    """

    def test_vm_storage_capacity(
        self,
        setup_cnv,
        project_factory,
        clone_vm_workload,
        cnv_workload,
        snapshot_factory,
    ):
        """
        Add capacity
            1. Keep IO operations going on in the VMs. Make sure some snapshot
             and clones of the VMs present.
            2. Keep vms in different states (power on, paused, stopped)
            3. Perform add capacity using the official documents
            4. Verify Cluster Stability:
               Ensure the cluster is stable after the adding extra capacity:
               All nodes are in a Ready state.
               All critical pods are running as expected.
            5. Check for data Integrity
            6. Ensure the additional storage has been added
            7. Check if all the snapshots and clones preserved their
            states and data integrity
        """
        # Pre-requisite Setup for TC
        proj_obj = project_factory()
        file_paths = ["/new_file.txt"]

        # Create VMs
        # Keep IO operations going on in the VMs
        vm_list = []
        i = 3
        while i > 0:
            vm_obj = cnv_workload(
                namespace=proj_obj.namespace, volume_interface=constants.VM_VOLUME_PVC
            )
            run_dd_io(vm_obj=vm_obj, file_path=file_paths[0])
            vm_list.append(vm_obj)
            i -= 1

        # Make sure some snapshot and clones of the VMs present.
        random_vm = random.sample(vm_list, 2)
        for index, vm_obj in enumerate(random_vm):
            vm_obj.stop()
            # Take snapshot of the VM's PVC
            pvc_obj = vm_obj.get_vm_pvc_obj()
            snapshot_factory(pvc_obj)
            vm_list.remove(vm_obj)

        # 1 running 2 stopped

        for index, vm_obj in enumerate(random_vm):
            res_vm_obj_clone = clone_vm_workload(vm_obj, namespace=vm_obj.namespace)

            run_dd_io(vm_obj=res_vm_obj_clone, file_path=file_paths[0])
            vm_list.append(res_vm_obj_clone)

        # 3 running 2 stopped

        # Keep VMs in different states (power on, paused, stopped)
        [vm_stop, vm_pause] = random.sample(vm_list, 2)
        vm_stop.stop()
        vm_pause.pause()

        # works til here

        # get osd pods restart count before
        osd_pods_restart_count_before = get_pod_restarts_count(
            label=constants.OSD_APP_LABEL
        )

        # add capacity to the cluster
        logger.info("Performing add capacity...")
        add_capacity(osd_size_capacity_requested=20)
        logger.info("Successfully added capacity")

        # Verify Cluster Stability:
        logger.info("Verifying cluster stability...")
        assert all_nodes_ready()
        assert all_critical_pods_running()

        # get osd pods restart count after
        osd_pods_restart_count_after = get_pod_restarts_count(
            label=constants.OSD_APP_LABEL
        )

        # assert if any osd pods restart
        assert sum(osd_pods_restart_count_before.values()) == sum(
            osd_pods_restart_count_after.values()
        ), "Some of the osd pods have restarted during the add capacity"
        logger.info("osd pod restarts counts are same before and after.")

        # assert if osd weights for both the zones are not balanced
        tools_pod = get_ceph_tools_pod()
        zone1_osd_weight = tools_pod.exec_sh_cmd_on_pod(
            command=f"ceph osd tree | grep 'zone {constants.DATA_ZONE_LABELS[0]}' | awk '{{print $2}}'",
        )
        zone2_osd_weight = tools_pod.exec_sh_cmd_on_pod(
            command=f"ceph osd tree | grep 'zone {constants.DATA_ZONE_LABELS[1]}' | awk '{{print $2}}'",
        )

        assert float(zone1_osd_weight.strip()) == float(
            zone2_osd_weight.strip()
        ), "OSD weights are not balanced"
        logger.info("OSD weights are balanced")
