"""Intenta crear el servidor Always Free del MDM en Oracle (Montreal) hasta que haya capacidad.
Idempotente: si ya existe una instancia mdm-rocketfuel viva, no crea otra."""
import os, sys, time, oci

NAME = "mdm-rocketfuel"
SIZES = ((2, 12), (1, 6))  # dentro del Always Free (2 OCPU / 12 GB)
WINDOW = int(os.environ.get("RETRY_WINDOW_SECONDS", "0"))  # GitHub espacia el cron: reintentar dentro de cada corrida
PAUSE = 60

cfg = oci.config.from_file(os.path.expanduser("~/.oci/config"))
t = cfg["tenancy"]
cc, vn = oci.core.ComputeClient(cfg), oci.core.VirtualNetworkClient(cfg)


def out(key, val):
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
        f.write(f"{key}={val}\n")


def public_ip(inst_id):
    va = cc.list_vnic_attachments(t, instance_id=inst_id).data
    return vn.get_vnic(va[0].vnic_id).data.public_ip if va else None


alive = [i for i in cc.list_instances(t, display_name=NAME).data
         if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
if alive:
    inst = alive[0]
    print("Ya existe:", inst.lifecycle_state)
    if inst.lifecycle_state == "RUNNING":
        out("ip", public_ip(inst.id) or "")
        out("shape", f"{int(inst.shape_config.ocpus)}/{int(inst.shape_config.memory_in_gbs)}")
    sys.exit(0)

vcn = [v for v in vn.list_vcns(t).data if v.lifecycle_state == "AVAILABLE"][0]
sub = vn.list_subnets(t, vcn_id=vcn.id).data[0]
img = [i for i in cc.list_images(t, operating_system="Canonical Ubuntu", operating_system_version="22.04",
                                  shape="VM.Standard.A1.Flex", sort_by="TIMECREATED", sort_order="DESC").data
       if "Minimal" not in i.display_name][0]
ad = oci.identity.IdentityClient(cfg).list_availability_domains(t).data[0].name

deadline = time.time() + WINDOW
while True:
    for ocpu, mem in SIZES:
        try:
            inst = cc.launch_instance(oci.core.models.LaunchInstanceDetails(
                availability_domain=ad, compartment_id=t, display_name=NAME, shape="VM.Standard.A1.Flex",
                shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=ocpu, memory_in_gbs=mem),
                source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=img.id, boot_volume_size_in_gbs=50),
                create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=sub.id, assign_public_ip=True),
                metadata={"ssh_authorized_keys": os.environ["SSH_PUB"]})).data
        except oci.exceptions.ServiceError as e:
            if "capacity" in (e.message or "").lower() or e.status in (429, 500):
                print(f"Sin capacidad {ocpu}/{mem}")
                continue
            raise
        inst = oci.wait_until(cc, cc.get_instance(inst.id), "lifecycle_state", "RUNNING", max_wait_seconds=900).data
        print(f"CREADA {ocpu}/{mem}")
        out("ip", public_ip(inst.id))
        out("shape", f"{ocpu}/{mem}")
        sys.exit(0)
    if time.time() + PAUSE > deadline:
        break
    time.sleep(PAUSE)
print("Sin capacidad en este intento; el siguiente corre con el cron.")
