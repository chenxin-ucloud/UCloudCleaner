#!/usr/bin/env python3
"""
聚合资源删除脚本
按顺序删除: uhost -> udisk -> natgw -> uni -> alb -> nlb -> eip -> ugn -> uwan -> ipsecvpn -> secgroup -> acl -> subnet -> vpc
删除失败时跳过并记录日志
"""

from ucloud.client import Client
import logging
import json
import os
import time

# 创建 logger（不在模块级别添加任何 handlers，由调用方负责配置）
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 禁用 ucloud 库的日志
ucloud_logger = logging.getLogger("ucloud")
ucloud_logger.disabled = True


def setup_file_logging():
    """配置文件日志 handler，仅在命令行直接运行时调用"""
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    info_log_file = os.path.join(log_dir, "info.log")
    error_log_file = os.path.join(log_dir, "error.log")

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    info_handler = logging.FileHandler(info_log_file, encoding='utf-8')
    info_handler.setLevel(logging.INFO)
    info_handler.addFilter(lambda record: record.levelno < logging.ERROR)
    info_handler.setFormatter(formatter)

    error_handler = logging.FileHandler(error_log_file, encoding='utf-8')
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.addHandler(info_handler)
    logger.addHandler(error_handler)
    logger.addHandler(console_handler)

    return info_log_file, error_log_file

def _load_projects_config():
    """从 sdk/config.json 加载项目配置，仅 CLI 模式使用"""
    config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if not os.path.exists(config_file):
        raise FileNotFoundError(
            f"未找到配置文件: {config_file}\n"
            "请复制 config.json.example 为 config.json 并填写公私钥和项目ID"
        )
    with open(config_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("projects", [])


def get_client(region, project_id, public_key, private_key, base_url=None):
    """创建 UCloud 客户端"""
    config = {
        "region": region,
        "project_id": project_id,
        "public_key": public_key,
        "private_key": private_key,
    }
    if base_url:
        config["base_url"] = base_url
    return Client(config)


def fetch_project_list(public_key, private_key, base_url=None):
    """获取项目列表，返回按 CreateTime 升序排列的项目列表。

    供 Web/GUI 在填写公钥私钥后自动拉取项目，供用户选择。
    调用失败时返回空列表并记录 ERROR 日志。

    返回格式: [{"project_id": "...", "name": "...", "create_time": 123}, ...]
    """
    config = {
        "public_key": public_key,
        "private_key": private_key,
    }
    if base_url:
        config["base_url"] = base_url
    try:
        client = Client(config)
        resp = client.uaccount().get_project_list()
        projects = resp.get("ProjectSet", [])
        # 按创建时间升序（最旧的在最前）
        sorted_projects = sorted(
            projects,
            key=lambda p: int(p.get("CreateTime") or 0),
            reverse=False,
        )
        return [
            {
                "project_id": p["ProjectId"],
                "name": p.get("ProjectName", p["ProjectId"]),
                "create_time": p.get("CreateTime"),
            }
            for p in sorted_projects
            if "ProjectId" in p
        ]
    except Exception as e:
        logger.error(f"获取项目列表失败: {e}")
        return []


def _interruptible_sleep(seconds, stop_event=None):
    """分段 sleep，每 0.5 秒检查一次停止信号；stop_event 为 None 时退化为普通 sleep"""
    if stop_event is None:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop_event.is_set():
            return
        time.sleep(0.5)


def _stopped(stop_event):
    return stop_event is not None and stop_event.is_set()


def delete_uhosts(client, loc_name, region, zone, project_id, stop_event=None):
    """删除 UHost 实例"""
    try:
        resp = client.uhost().describe_uhost_instance()
        uhosts = [uhost['UHostId'] for uhost in resp.get('UHostSet', []) if 'UHostId' in uhost]
        terminated_any = False
        for uhostid in uhosts:
            if _stopped(stop_event):
                return
            try:
                logger.info(f"[{loc_name}] 正在关闭项目:{project_id} 下的UHost: {uhostid}...")
                client.uhost().poweroff_uhost_instance({'UHostId': uhostid})
                _interruptible_sleep(10, stop_event)
                describe_resp = client.uhost().describe_uhost_instance({'UHostId': uhostid})
                state = describe_resp['UHostSet'][0].get('State')
                if state == 'Stopped':
                    client.uhost().terminate_uhost_instance({
                        'UHostId': uhostid,
                        "ReleaseEIP": "true",
                        "ReleaseUDisk": "true",
                    })
                    time.sleep(1)
                    terminated_any = True
                    logger.info(f"[{loc_name}] 项目:{project_id} 下的UHost: {uhostid} 已删除")
                else:
                    logger.warning(f"[{loc_name}] 项目:{project_id} UHost: {uhostid} 状态为 {state}，跳过删除")
            except Exception as e:
                logger.error(f"[{loc_name}] 删除UHost {uhostid} 失败: {e}")

        # UHost 销毁后云端异步卸载关联数据盘，等待一段时间避免后续删除 UDisk 时报错
        if terminated_any:
            wait_seconds = 15
            logger.info(f"[{loc_name}] 项目:{project_id} 等待 {wait_seconds} 秒，确保数据盘完成卸载...")
            _interruptible_sleep(wait_seconds, stop_event)
    except Exception as e:
        logger.error(f"[{loc_name}] 获取UHost列表失败: {e}")


def delete_udisks(client, loc_name, region, zone, project_id, stop_event=None):
    """删除 UDisk 实例"""
    try:
        resp = client.udisk().describe_udisk()
        udisks = [udisk['UDiskId'] for udisk in resp.get('DataSet', []) if 'UDiskId' in udisk]
        for udiskid in udisks:
            if _stopped(stop_event):
                return
            try:
                logger.info(f"[{loc_name}] 项目: {project_id} 正在删除 UDisk: {udiskid}...")
                client.udisk().delete_udisk({'UDiskId': udiskid, "Zone": zone})
                time.sleep(1)
                logger.info(f"[{loc_name}] 项目: {project_id} 删除 UDisk: {udiskid} 成功")
            except Exception as e:
                logger.error(f"[{loc_name}] 删除UDisk {udiskid} 失败: {e}")
    except Exception as e:
        logger.error(f"[{loc_name}] 获取UDisk列表失败: {e}")


def delete_natgws(client, loc_name, region, zone, project_id, stop_event=None):
    """删除 NAT 网关"""
    try:
        resp = client.vpc().describe_natgw()
        natgws = [natgw['NATGWId'] for natgw in resp.get("DataSet", []) if 'NATGWId' in natgw]
        for natgwid in natgws:
            if _stopped(stop_event):
                return
            try:
                logger.info(f"[{loc_name}] 项目: {project_id} 正在删除 NATGW: {natgwid}...")
                client.vpc().delete_natgw({'NATGWId': natgwid, "ReleaseEip": "true"})
                time.sleep(1)
                logger.info(f"[{loc_name}] 项目: {project_id} 删除 NATGW: {natgwid} 成功")
            except Exception as e:
                logger.error(f"[{loc_name}] 删除NATGW {natgwid} 失败: {e}")
    except Exception as e:
        logger.error(f"[{loc_name}] 获取NATGW列表失败: {e}")


def delete_unis(client, loc_name, region, zone, project_id, stop_event=None):
    """删除 UNI 虚拟网卡"""
    try:
        resp = client.vpc().describe_network_interface()
        unis = [uni['InterfaceId'] for uni in resp.get("NetworkInterfaceSet", []) if 'InterfaceId' in uni]
        for uniid in unis:
            if _stopped(stop_event):
                return
            try:
                logger.info(f"[{loc_name}] 项目: {project_id} 正在删除 UNI: {uniid}...")
                client.vpc().delete_network_interface({'InterfaceId': uniid})
                time.sleep(1)
                logger.info(f"[{loc_name}] 项目: {project_id} 删除 UNI: {uniid} 成功")
            except Exception as e:
                logger.error(f"[{loc_name}] 删除UNI {uniid} 失败: {e}")
    except Exception as e:
        logger.error(f"[{loc_name}] 获取UNI列表失败: {e}")


def delete_albs(client, loc_name, region, zone, project_id, stop_event=None):
    """删除 ALB 负载均衡"""
    try:
        resp = client.ulb().describe_load_balancers()
        loadbalancers = [lb['LoadBalancerId'] for lb in resp.get("LoadBalancers", []) if 'LoadBalancerId' in lb]
        for lbid in loadbalancers:
            if _stopped(stop_event):
                return
            try:
                logger.info(f"[{loc_name}] 项目: {project_id} 正在删除 ALB: {lbid}...")
                client.ulb().delete_load_balancer({'LoadBalancerId': lbid})
                time.sleep(1)
                logger.info(f"[{loc_name}] 项目: {project_id} 删除 ALB: {lbid} 成功")
            except Exception as e:
                logger.error(f"[{loc_name}] 删除ALB {lbid} 失败: {e}")
    except Exception as e:
        logger.error(f"[{loc_name}] 获取ALB列表失败: {e}")


def delete_nlbs(client, loc_name, region, zone, project_id, stop_event=None):
    """删除 NLB 网络负载均衡"""
    try:
        resp = client.nlb().describe_network_load_balancers()
        nlbs = [lb['NetworkLoadBalancerId'] for lb in resp.get('NetworkLoadBalancers', []) if 'NetworkLoadBalancerId' in lb]
        for nlbid in nlbs:
            if _stopped(stop_event):
                return
            try:
                logger.info(f"[{loc_name}] 项目: {project_id} 正在删除 NLB: {nlbid}...")
                client.nlb().delete_network_load_balancer({'NetworkLoadBalancerId': nlbid})
                time.sleep(1)
                logger.info(f"[{loc_name}] 项目: {project_id} 删除 NLB: {nlbid} 成功")
            except Exception as e:
                logger.error(f"[{loc_name}] 删除NLB {nlbid} 失败: {e}")
    except Exception as e:
        error_msg = str(e)
        if 'Service unavailable' in error_msg or '150' in error_msg:
            logger.info(f"[{loc_name}] NLB 服务不可用，跳过")
        else:
            logger.error(f"[{loc_name}] 获取NLB列表失败: {e}")


def delete_ugns(client, loc_name, region, zone, project_id, stop_event=None):
    """删除 UGN 云联网"""
    try:
        # 查询 UGN 列表
        resp = client.ugn().list_ugn()
        ugns = resp.get('UGNs', [])
        ugn_ids = [ugn['UGNID'] for ugn in ugns if 'UGNID' in ugn]

        # 第一步：删除 UGN 绑定的带宽包
        for ugnid in ugn_ids:
            if _stopped(stop_event):
                return
            try:
                bw_resp = client.ugn().get_simple_ugn_bw_packages({'UGNID': ugnid})
                bw_packages = bw_resp.get('BwPackages', [])
                package_ids = [pkg['PackageID'] for pkg in bw_packages if 'PackageID' in pkg]

                if package_ids:
                    for pkg_id in package_ids:
                        if _stopped(stop_event):
                            return
                        # 默认带宽包（default- 开头）不可删除，跳过
                        if pkg_id.startswith('default-'):
                            logger.info(f"[{loc_name}] 项目: {project_id} UGN {ugnid} 带宽包 {pkg_id} 为默认带宽包，跳过删除")
                            continue
                        try:
                            logger.info(f"[{loc_name}] 项目: {project_id} UGN {ugnid} 正在删除带宽包: {pkg_id}...")
                            client.ugn().invoke('DeleteUGNBwPackage', {
                                'UGNID': ugnid,
                                'BwPackageID': pkg_id
                            })
                            time.sleep(1)
                            logger.info(f"[{loc_name}] 项目: {project_id} UGN {ugnid} 删除带宽包: {pkg_id} 成功")
                        except Exception as e:
                            logger.warning(f"[{loc_name}] 项目: {project_id} UGN {ugnid} 删除带宽包 {pkg_id} 失败: {e}")
                else:
                    logger.info(f"[{loc_name}] 项目: {project_id} UGN {ugnid} 没有绑定带宽包")
            except Exception as e:
                logger.error(f"[{loc_name}] 项目: {project_id} 获取 UGN {ugnid} 带宽包列表失败: {e}")

        # 第二步：解绑 UGN 绑定的网络实例
        for ugnid in ugn_ids:
            if _stopped(stop_event):
                return
            try:
                # 获取 UGN 绑定的网络实例
                networks_resp = client.ugn().describe_simple_ugn({'UGNID': ugnid})
                networks = networks_resp.get('Networks', [])
                network_ids = [net['NetworkID'] for net in networks if 'NetworkID' in net]

                if network_ids:
                    logger.info(f"[{loc_name}] 项目: {project_id} UGN {ugnid} 绑定了 {len(network_ids)} 个网络实例，正在解绑...")
                    try:
                        # 使用 invoke 方法调用 DetachUGNNetworks
                        client.ugn().invoke('DetachUGNNetworks', {
                            'UGNID': ugnid,
                            'Networks': network_ids
                        })
                        time.sleep(2)
                        logger.info(f"[{loc_name}] 项目: {project_id} UGN {ugnid} 解绑网络实例成功")
                    except Exception as e:
                        logger.warning(f"[{loc_name}] 项目: {project_id} UGN {ugnid} 解绑网络实例失败: {e}")
                else:
                    logger.info(f"[{loc_name}] 项目: {project_id} UGN {ugnid} 没有绑定网络实例")
            except Exception as e:
                logger.error(f"[{loc_name}] 项目: {project_id} 获取 UGN {ugnid} 网络实例列表失败: {e}")

        # 第三步：删除所有 UGN
        for ugnid in ugn_ids:
            if _stopped(stop_event):
                return
            try:
                logger.info(f"[{loc_name}] 项目: {project_id} 正在删除 UGN: {ugnid}...")
                # 使用 invoke 方法调用 DelUGN
                client.ugn().invoke('DelUGN', {'UGNID': ugnid})
                time.sleep(1)
                logger.info(f"[{loc_name}] 项目: {project_id} 删除 UGN: {ugnid} 成功")
            except Exception as e:
                logger.error(f"[{loc_name}] 删除UGN {ugnid} 失败: {e}")
    except Exception as e:
        logger.error(f"[{loc_name}] 获取UGN列表失败: {e}")


def delete_eips(client, loc_name, region, zone, project_id, stop_event=None):
    """删除 EIP 弹性IP - 先解绑再删除"""
    try:
        resp = client.unet().describe_eip()
        eips = resp.get('EIPSet', [])
        for eip in eips:
            if _stopped(stop_event):
                return
            eipid = eip.get('EIPId')
            if not eipid:
                continue
            try:
                # 检查 EIP 绑定关系
                resource = eip.get('Resource', {})
                resource_type = resource.get('ResourceType')
                resource_id = resource.get('ResourceId') or resource.get('ResourceID')
                status = eip.get('Status')

                if resource_type and status == 'used':
                    logger.info(f"[{loc_name}] 项目: {project_id} EIP {eipid} 绑定到 {resource_type} (ResourceId: {resource_id})，正在解绑...")
                    try:
                        # 如果eip未绑定任何资源，则使用ResourceId为空进行解绑
                        if resource_id:
                            client.unet().un_bind_eip({
                                'EIPId': eipid,
                                'ResourceType': resource_type,
                                'ResourceId': resource_id
                            })
                        else:
                            client.unet().un_bind_eip({
                                'EIPId': eipid,
                                'ResourceType': resource_type,
                                'ResourceId': ''
                                })
                        time.sleep(2)
                        logger.info(f"[{loc_name}] 项目: {project_id} EIP {eipid} 解绑成功")
                    except Exception as e:
                        logger.warning(f"[{loc_name}] 项目: {project_id} EIP {eipid} 解绑失败: {e}")
                        continue

                logger.info(f"[{loc_name}] 项目: {project_id} 正在删除 EIP: {eipid}...")
                client.unet().release_eip({'EIPId': eipid})
                time.sleep(1)
                logger.info(f"[{loc_name}] 项目: {project_id} 删除 EIP: {eipid} 成功")
            except Exception as e:
                logger.error(f"[{loc_name}] 删除EIP {eipid} 失败: {e}")
    except Exception as e:
        logger.error(f"[{loc_name}] 获取EIP列表失败: {e}")


def delete_uwans(client, loc_name, region, zone, project_id, stop_event=None):
    """删除 UWAN 资源 - CE网关和POPGW虚拟路由器"""
    # 第一步：删除 CE 网关
    try:
        resp = client.uwsc().invoke('DescribeCEGateway', {'Backend': 'UWSC'})
        ce_gateways = [vpn.get('VPNId') for vpn in (resp.get('VPNInfos') or []) if vpn.get('VPNId')]

        if ce_gateways:
            logger.info(f"[{loc_name}] 项目: {project_id} 找到 {len(ce_gateways)} 个 CE 网关: {ce_gateways}")
            for vpnid in ce_gateways:
                if _stopped(stop_event):
                    return
                try:
                    logger.info(f"[{loc_name}] 项目: {project_id} 正在删除 CE 网关: {vpnid}...")
                    client.uwsc().invoke('DeleteCEGateway', {'VPNId': vpnid, 'Backend': 'UWSC'})
                    time.sleep(1)
                    logger.info(f"[{loc_name}] 项目: {project_id} 删除 CE 网关: {vpnid} 成功")
                except Exception as e:
                    logger.error(f"[{loc_name}] 删除CE网关 {vpnid} 失败: {e}")
    except Exception as e:
        logger.error(f"[{loc_name}] 获取CE网关列表失败: {e}")

    # 第二步：删除 POPGW 虚拟路由器
    try:
        resp = client.uwsc().invoke('DescribePOPGW', {'Backend': 'UWSC'})
        popgws = [pg.get('PopGwId') for pg in (resp.get('POPGWInfos') or []) if pg.get('PopGwId')]

        if popgws:
            logger.info(f"[{loc_name}] 项目: {project_id} 找到 {len(popgws)} 个 POPGW: {popgws}")
            for popgwid in popgws:
                if _stopped(stop_event):
                    return
                try:
                    logger.info(f"[{loc_name}] 项目: {project_id} 正在删除 POPGW: {popgwid}...")
                    client.uwsc().invoke('DeletePOPGW', {'PopGwId': popgwid, 'Backend': 'UWSC'})
                    time.sleep(1)
                    logger.info(f"[{loc_name}] 项目: {project_id} 删除 POPGW: {popgwid} 成功")
                except Exception as e:
                    logger.error(f"[{loc_name}] 删除POPGW {popgwid} 失败: {e}")
    except Exception as e:
        logger.error(f"[{loc_name}] 获取POPGW列表失败: {e}")


def delete_ipsecvpns(client, loc_name, region, zone, project_id, stop_event=None):
    """删除 IPSecVPN 资源 - 隧道、VPN网关和客户VPN网关"""
    # 第一步：删除 VPN 隧道（隧道依附于 VPN 网关，须先删除）
    try:
        resp = client.ipsecvpn().describe_vpn_tunnel()
        tunnels = [t['VPNTunnelId'] for t in (resp.get('DataSet') or []) if 'VPNTunnelId' in t]

        if tunnels:
            logger.info(f"[{loc_name}] 项目: {project_id} 找到 {len(tunnels)} 条 VPN 隧道: {tunnels}")
            for tunnelid in tunnels:
                if _stopped(stop_event):
                    return
                try:
                    logger.info(f"[{loc_name}] 项目: {project_id} 正在删除 VPN 隧道: {tunnelid}...")
                    client.ipsecvpn().delete_vpn_tunnel({'VPNTunnelId': tunnelid})
                    time.sleep(1)
                    logger.info(f"[{loc_name}] 项目: {project_id} 删除 VPN 隧道: {tunnelid} 成功")
                except Exception as e:
                    logger.error(f"[{loc_name}] 删除VPN隧道 {tunnelid} 失败: {e}")
    except Exception as e:
        logger.error(f"[{loc_name}] 获取VPN隧道列表失败: {e}")

    # 第二步：删除 VPN 网关（顺带解绑并释放绑定的 EIP）
    try:
        resp = client.ipsecvpn().describe_vpn_gateway()
        gateways = [g['VPNGatewayId'] for g in (resp.get('DataSet') or []) if 'VPNGatewayId' in g]

        if gateways:
            logger.info(f"[{loc_name}] 项目: {project_id} 找到 {len(gateways)} 个 VPN 网关: {gateways}")
            for gwid in gateways:
                if _stopped(stop_event):
                    return
                try:
                    logger.info(f"[{loc_name}] 项目: {project_id} 正在删除 VPN 网关: {gwid}...")
                    client.ipsecvpn().delete_vpn_gateway({'VPNGatewayId': gwid, 'ReleaseEip': True})
                    time.sleep(1)
                    logger.info(f"[{loc_name}] 项目: {project_id} 删除 VPN 网关: {gwid} 成功")
                except Exception as e:
                    logger.error(f"[{loc_name}] 删除VPN网关 {gwid} 失败: {e}")
    except Exception as e:
        logger.error(f"[{loc_name}] 获取VPN网关列表失败: {e}")

    # 第三步：删除客户 VPN 网关
    try:
        resp = client.ipsecvpn().describe_remote_vpn_gateway()
        remote_gateways = [g['RemoteVPNGatewayId'] for g in (resp.get('DataSet') or []) if 'RemoteVPNGatewayId' in g]

        if remote_gateways:
            logger.info(f"[{loc_name}] 项目: {project_id} 找到 {len(remote_gateways)} 个客户 VPN 网关: {remote_gateways}")
            for rgwid in remote_gateways:
                if _stopped(stop_event):
                    return
                try:
                    logger.info(f"[{loc_name}] 项目: {project_id} 正在删除客户 VPN 网关: {rgwid}...")
                    client.ipsecvpn().delete_remote_vpn_gateway({'RemoteVPNGatewayId': rgwid})
                    time.sleep(1)
                    logger.info(f"[{loc_name}] 项目: {project_id} 删除客户 VPN 网关: {rgwid} 成功")
                except Exception as e:
                    logger.error(f"[{loc_name}] 删除客户VPN网关 {rgwid} 失败: {e}")
    except Exception as e:
        logger.error(f"[{loc_name}] 获取客户VPN网关列表失败: {e}")


def delete_secgroups(client, loc_name, region, zone, project_id, stop_event=None):
    """删除安全组"""
    try:
        resp = client.vpc().describe_sec_group()
        secgroups = [sg['SecGroupId'] for sg in resp.get("DataSet", []) if 'SecGroupId' in sg]
        for sgid in secgroups:
            if _stopped(stop_event):
                return
            try:
                logger.info(f"[{loc_name}] 项目: {project_id} 正在删除安全组: {sgid}...")
                client.vpc().delete_sec_group({'SecGroupId': [sgid]})
                time.sleep(1)
                logger.info(f"[{loc_name}] 项目: {project_id} 删除安全组: {sgid} 成功")
            except Exception as e:
                logger.error(f"[{loc_name}] 删除安全组 {sgid} 失败: {e}")
    except Exception as e:
        logger.error(f"[{loc_name}] 获取安全组列表失败: {e}")


def delete_acls(client, loc_name, region, zone, project_id, stop_event=None):
    """删除网络 ACL - 先解除子网绑定再删除"""
    try:
        resp = client.vpc().describe_network_acl()
        # API 返回的是 AclList 而不是 DataSet
        acls = resp.get("AclList", [])
        for acl in acls:
            if _stopped(stop_event):
                return
            aclid = acl.get('AclId')
            if not aclid:
                continue
            try:
                # 先解除关联的子网绑定
                associations = acl.get('Associations', [])
                if associations:
                    for assoc in associations:
                        subnet_id = assoc.get('SubnetworkId')
                        assoc_id = assoc.get('AssociationId')
                        if subnet_id:
                            try:
                                logger.info(f"[{loc_name}] 项目: {project_id} 正在解除 ACL {aclid} 与子网 {subnet_id} 的绑定...")
                                # 使用 SubnetworkId 解除绑定
                                client.vpc().delete_network_acl_association({
                                    'AclId': aclid,
                                    'SubnetworkId': subnet_id
                                })
                                time.sleep(1)
                                logger.info(f"[{loc_name}] 项目: {project_id} 解除 ACL {aclid} 与子网 {subnet_id} 绑定成功")
                            except Exception as e:
                                logger.warning(f"[{loc_name}] 解除 ACL {aclid} 与子网 {subnet_id} 绑定失败: {e}")

                # 删除 ACL - 使用 AclId 字段
                logger.info(f"[{loc_name}] 项目: {project_id} 正在删除 ACL: {aclid}...")
                client.vpc().delete_network_acl({'AclId': aclid})
                time.sleep(1)
                logger.info(f"[{loc_name}] 项目: {project_id} 删除 ACL: {aclid} 成功")
            except Exception as e:
                logger.error(f"[{loc_name}] 删除ACL {aclid} 失败: {e}")
    except Exception as e:
        error_msg = str(e)
        if 'Service unavailable' in error_msg or '150' in error_msg:
            logger.info(f"[{loc_name}] ACL 服务不可用，跳过")
        else:
            logger.error(f"[{loc_name}] 获取ACL列表失败: {e}")


def delete_subnets(client, loc_name, region, zone, project_id, stop_event=None):
    """删除子网（先释放子网关联的内网 VIP）"""
    try:
        resp = client.vpc().describe_subnet()
        subnets = resp.get("DataSet", [])
        subnet_ids = [subnet['SubnetId'] for subnet in subnets if 'SubnetId' in subnet]

        # 第一步：查询并释放内网 VIP（VIP 依附于子网，须先释放才能删除子网）
        vpc_ids = {subnet.get('VPCId') for subnet in subnets if 'VPCId' in subnet}
        for vpcid in vpc_ids:
            if _stopped(stop_event):
                return
            try:
                vip_resp = client.vpc().describe_vip({'VPCId': vpcid})
                vips = vip_resp.get('VIPSet', [])
                vip_ids = [vip['VIPId'] for vip in vips if 'VIPId' in vip]
                for vipid in vip_ids:
                    if _stopped(stop_event):
                        return
                    try:
                        logger.info(f"[{loc_name}] 项目: {project_id} 正在释放内网 VIP: {vipid}...")
                        client.vpc().release_vip({'VIPId': vipid})
                        time.sleep(1)
                        logger.info(f"[{loc_name}] 项目: {project_id} 释放内网 VIP: {vipid} 成功")
                    except Exception as e:
                        logger.warning(f"[{loc_name}] 释放内网 VIP {vipid} 失败: {e}")
            except Exception as e:
                logger.error(f"[{loc_name}] 获取 VPC {vpcid} 内网 VIP 列表失败: {e}")

        # 第二步：删除子网
        for subnetid in subnet_ids:
            if _stopped(stop_event):
                return
            try:
                logger.info(f"[{loc_name}] 项目: {project_id} 正在删除子网: {subnetid}...")
                client.vpc().delete_subnet({'SubnetId': subnetid})
                time.sleep(1)
                logger.info(f"[{loc_name}] 项目: {project_id} 删除子网: {subnetid} 成功")
            except Exception as e:
                logger.error(f"[{loc_name}] 删除子网 {subnetid} 失败: {e}")
    except Exception as e:
        logger.error(f"[{loc_name}] 获取子网列表失败: {e}")


def delete_vpcs(client, loc_name, region, zone, project_id, stop_event=None):
    """删除 VPC"""
    try:
        resp = client.vpc().describe_vpc()
        vpcs = [vpc['VPCId'] for vpc in resp.get("DataSet", []) if 'VPCId' in vpc]
        for vpcid in vpcs:
            if _stopped(stop_event):
                return
            try:
                # 先删除 VPC 互通
                try:
                    intercom_resp = client.vpc().describe_vpc_intercom({"VPCId": vpcid})
                    intercom_vpcs = [vpc['VPCId'] for vpc in intercom_resp.get("DataSet", []) if 'VPCId' in vpc]
                    for intercom_vpc in intercom_vpcs:
                        logger.info(f"[{loc_name}] 项目: {project_id} 正在删除 VPC 互通: {vpcid} <-> {intercom_vpc}...")
                        client.vpc().delete_vpc_intercom({"VPCId": vpcid, "DstVPCId": intercom_vpc})
                        time.sleep(1)
                except Exception as e:
                    logger.warning(f"[{loc_name}] 获取/删除VPC互通失败: {e}")

                logger.info(f"[{loc_name}] 项目: {project_id} 正在删除 VPC: {vpcid}...")
                client.vpc().delete_vpc({'VPCId': vpcid})
                time.sleep(1)
                logger.info(f"[{loc_name}] 项目: {project_id} 删除 VPC: {vpcid} 成功")
            except Exception as e:
                logger.error(f"[{loc_name}] 删除VPC {vpcid} 失败: {e}")
    except Exception as e:
        logger.error(f"[{loc_name}] 获取VPC列表失败: {e}")


# 删除操作列表（模块级别，供 GUI 工具引用）
DELETE_OPERATIONS = [
    ("UHost", delete_uhosts),
    ("UDisk", delete_udisks),
    ("NATGW", delete_natgws),
    ("UNI", delete_unis),
    ("ALB", delete_albs),
    ("NLB", delete_nlbs),
    ("EIP", delete_eips),
    ("UGN", delete_ugns),
    ("UWAN", delete_uwans),
    ("IPSecVPN", delete_ipsecvpns),
    ("SecurityGroup", delete_secgroups),
    ("ACL", delete_acls),
    ("Subnet", delete_subnets),
    ("VPC", delete_vpcs),
]


def main():
    """主函数"""
    info_log_file, error_log_file = setup_file_logging()

    projects_config = _load_projects_config()

    # 加载区域配置（统一从项目根目录 assets/ 读取）
    sdk_dir = os.path.dirname(os.path.abspath(__file__))
    region_file = os.path.join(sdk_dir, "..", "assets", "region.json")
    with open(region_file, "r", encoding="utf-8") as f:
        regions = json.load(f)

    logger.info("=" * 60)
    logger.info("开始批量删除资源")
    logger.info("=" * 60)

    for loc_name, loc in regions.items():
        region = loc.get("Region")
        zone = loc.get("Zone")
        logger.info(f"\n处理区域: {loc_name} ({region})")

        for config in projects_config:
            projects = config["project_ids"]
            public_key = config["public_key"]
            private_key = config["private_key"]

            for project_id in projects:
                client = get_client(region, project_id, public_key, private_key)

                for resource_name, delete_func in DELETE_OPERATIONS:
                    try:
                        delete_func(client, loc_name, region, zone, project_id)
                    except Exception as e:
                        logger.error(f"[{loc_name}] 处理 {resource_name} 时发生错误: {e}")
                        continue

    logger.info("\n" + "=" * 60)
    logger.info("批量删除资源完成")
    logger.info(f"Info 日志文件: {info_log_file}")
    logger.info(f"Error 日志文件: {error_log_file}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
