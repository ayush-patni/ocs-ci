import logging
import random
import pytest
from ocs_ci.framework.pytest_customization.marks import magenta_squad, workloads
from ocs_ci.framework.testlib import E2ETest
from ocs_ci.helpers.cnv_helpers import (
    run_dd_io,
    all_nodes_ready,
    cal_md5sum_vm,
)
from ocs_ci.helpers.keyrotation_helper import PVKeyrotation
from ocs_ci.ocs import constants
from ocs_ci.ocs.resources import storage_cluster
from ocs_ci.ocs.resources.pod import get_ceph_tools_pod, get_pod_restarts_count

logger = logging.getLogger(__name__)


@magenta_squad
@workloads
@pytest.mark.polarion_id("OCS-")
class TestVmStorageCapacity(E2ETest):
    """
    Perform add capacity operation while the VMs are in different states
    and in the presence of snapshots and clones of the VMs.
    """

    def test_vm_storage_capacity(
        self,
        # setup_cnv,
        pv_encryption_kms_setup_factory,
        storageclass_factory,
        project_factory,
        cnv_workload,
        clone_vm_workload,
        snapshot_factory,
    ):
        """
        Test steps:
        1. Keep IO operations going on VMs, with snapshots and clones present.
        2. Keep VMs in different states (power on, paused, stopped).
        3. Perform add capacity using official docs.
        4. Verify Cluster Stability and Data Integrity.
        5. Ensure the additional storage has been added.
        6. Verify snapshots and clones have preserved data integrity.
        """
        source_csum = {}
        res_csum = {}
        vm_list_clone = []
        vm_list = []
        i = 3
        # Create ceph-csi-kms-token in the tenant namespace
        proj_obj = project_factory()
        file_paths = ["/source_file.txt", "/new_file.txt"]

        # Setup csi-kms-connection-details configmap
        logger.info("Setting up csi-kms-connection-details configmap")
        kms = pv_encryption_kms_setup_factory(kv_version="v2")
        logger.info("csi-kms-connection-details setup successful")

        # Create an encryption enabled storageclass for RBD
        sc_obj_def = storageclass_factory(
            interface=constants.CEPHBLOCKPOOL,
            encrypted=True,
            encryption_kms_id=kms.kmsid,
            new_rbd_pool=True,
            mapOptions="krbd:rxbounce",
            mounter="rbd",
        )

        kms.vault_path_token = kms.generate_vault_token()
        kms.create_vault_csi_kms_token(namespace=proj_obj.namespace)

        pvk_obj = PVKeyrotation(sc_obj_def)
        pvk_obj.annotate_storageclass_key_rotation(schedule="*/3 * * * *")

        # Create a PVC-based VM (VM1)
        while i > 0:
            vm_obj = cnv_workload(
                storageclass=sc_obj_def.name,
                namespace=proj_obj.namespace,
                volume_interface=constants.VM_VOLUME_PVC,
            )
            vm_list.append(vm_obj)
            i -= 1
            source_csum[f"{vm_obj.name}"] = run_dd_io(
                vm_obj=vm_obj, file_path=file_paths[0], verify=True
            )
            clone = clone_vm_workload(vm_obj, namespace=vm_obj.namespace)
            res_csum[f"{vm_obj.name}"] = cal_md5sum_vm(
                vm_obj=clone, file_path=file_paths[0]
            )
            run_dd_io(vm_obj=clone, file_path=file_paths[0])
            vm_list_clone.append(vm_obj)

        random_vm = random.sample(vm_list, 2)
        for vm_obj in random_vm:
            vm_obj.stop()
            pvc_obj = vm_obj.get_vm_pvc_obj()
            snapshot_factory(pvc_obj)
            vm_list.remove(vm_obj)

        # Keep VMs in different states
        vm_pause = random.sample(vm_list, 2)
        for vm in vm_pause:
            vm.pause()

        # Verify the cluster's stability
        logger.info("Verifying cluster stability...")
        assert all_nodes_ready(), "Some nodes are not ready!"

        # get osd pods restart count before
        osd_pods_restart_count_before = get_pod_restarts_count(
            label=constants.OSD_APP_LABEL
        )

        # add capacity to the cluster
        storage_cluster.add_capacity_lso(ui_flag=False)
        logger.info("Successfully added capacity")

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

        # Verify data integrity for cloned VMs
        for vm_obj in vm_list:
            source_checksum = source_csum.get(vm_obj.name)
            result_checksum = res_csum.get(vm_obj.name)
            assert (
                source_checksum == result_checksum
            ), f"Failed: MD5 comparison between source {vm_obj.name} and its cloned VMs"
            vm_obj.stop()

        for vm_obj in vm_list_clone:
            vm_obj.stop()
