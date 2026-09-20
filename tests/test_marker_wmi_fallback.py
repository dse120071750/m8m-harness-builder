import platform
import sys
import pytest
import support  # noqa: F401
import runtime_release


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows WMI path')
def test_marker_environment_does_not_call_wmi_and_restores_hook(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('WMI must not be queried')
    monkeypatch.setattr(platform, '_wmi_query', forbidden)
    monkeypatch.setattr(platform, '_uname_cache', None)
    from packaging.markers import default_environment, Marker
    with runtime_release._local_windows_marker_queries():
        env = default_environment()
        assert env['sys_platform'] == 'win32'
        assert env['platform_system'] == 'Windows'
        assert Marker('sys_platform == "win32"').evaluate(env)
    assert platform._wmi_query is forbidden


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows WMI path')
def test_hook_restored_after_error():
    original = platform._wmi_query
    with pytest.raises(RuntimeError):
        with runtime_release._local_windows_marker_queries():
            raise RuntimeError('fixture failure')
    assert platform._wmi_query is original
