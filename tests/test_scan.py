"""Tests for AST inventory, exclusion rules, dependency graph, prioritization."""

from __future__ import annotations

from loki.scan import ast, exclude, graph, prioritize

USER_SERVICE = """
package com.acme.svc;

import com.acme.repo.UserRepository;
import java.time.Clock;
import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Autowired;

@Service
public class UserService {
    private final UserRepository repository;
    private final Clock clock;

    @Autowired
    public UserService(UserRepository repository, Clock clock) {
        this.repository = repository;
        this.clock = clock;
    }

    public User find(Long id) {
        if (id == null) {
            throw new IllegalArgumentException("id must not be null");
        }
        return repository.findById(id);
    }

    public int count() {
        return repository.count();
    }
}
"""

USER_REPOSITORY = """
package com.acme.repo;
public interface UserRepository {
    User findById(Long id);
    int count();
}
"""

CONFIG = """
package com.acme.config;
import org.springframework.context.annotation.Configuration;
@Configuration
public class AppConfig {
    public String beanName() { return "x"; }
}
"""

DTO = """
package com.acme.dto;
public class UserDto {
    private String name;
    public String getName() { return name; }
    public void setName(String name) { this.name = name; }
}
"""


def parse_one(src: str) -> ast.ClassInfo:
    infos = ast.parse_java_source(src, "X.java")
    assert len(infos) == 1
    return infos[0]


def test_parse_service_extracts_structure() -> None:
    info = parse_one(USER_SERVICE)
    assert info.fqcn == "com.acme.svc.UserService"
    assert info.kind == "class"
    assert info.stereotype == "Service"
    assert info.constructor_param_types == ["UserRepository", "Clock"]
    assert {m.name for m in info.public_methods} == {"find", "count"}
    assert info.complexity >= 1  # the null-check branch


def test_regex_fallback_extracts_same_essentials() -> None:
    # Directly exercise the fallback used when javalang cannot parse Java 21.
    info = ast._from_regex(USER_SERVICE, "X.java", "com.acme.svc")[0]
    assert info.name == "UserService"
    assert info.constructor_param_types == ["UserRepository", "Clock"]
    assert "Service" in info.annotations
    assert info.complexity >= 1


def test_exclude_config_and_interface_and_dto() -> None:
    config = parse_one(CONFIG)
    assert exclude.is_excluded(config, "com/acme/config/AppConfig.java", [])[0]

    repo = parse_one(USER_REPOSITORY)
    assert exclude.is_excluded(repo, "com/acme/repo/UserRepository.java", [])[0]

    dto = parse_one(DTO)
    assert exclude.is_excluded(dto, "com/acme/dto/UserDto.java", [])[0]


def test_service_is_not_excluded() -> None:
    info = parse_one(USER_SERVICE)
    excluded, reason = exclude.is_excluded(info, "com/acme/svc/UserService.java", ["**/dto/**"])
    assert not excluded, reason


def test_glob_exclusion_matches_nested_path() -> None:
    info = parse_one(USER_SERVICE)
    excluded, _ = exclude.is_excluded(
        info, "app/src/main/java/com/acme/dto/Thing.java", ["**/dto/**"]
    )
    assert excluded
    assert not exclude._glob_match("**/dto/**", "com/acme/svc/UserService.java")
    assert exclude._glob_match("**/*MapperImpl.java", "com/acme/map/UserMapperImpl.java")


def test_collaborators_resolve_only_project_types() -> None:
    classes = [parse_one(USER_SERVICE), parse_one(USER_REPOSITORY)]
    index = graph.build_index(classes)
    collabs = graph.collaborators_for(classes[0], index)
    # UserRepository is a project type; Clock (JDK) is not a mock target.
    assert [c.fqcn for c in collabs] == ["com.acme.repo.UserRepository"]
    assert any("findById" in s for s in collabs[0].signatures)


def test_prioritize_orders_by_gap_times_risk() -> None:
    service = parse_one(USER_SERVICE)  # Service stereotype, has a branch
    dto = parse_one(DTO)  # low risk, no branches
    ranked = prioritize.prioritize([(service, 0.30), (dto, 0.30)], target_coverage=0.90)
    assert ranked[0].info.name == "UserService"
    assert ranked[0].score > ranked[1].score


def test_discover_modules(tmp_path) -> None:
    (tmp_path / "app" / "src" / "main" / "java" / "com").mkdir(parents=True)
    (tmp_path / "core" / "src" / "main" / "java").mkdir(parents=True)
    modules = ast.discover_modules(tmp_path)
    names = {m.name for m in modules}
    assert names == {"app", "core"}
